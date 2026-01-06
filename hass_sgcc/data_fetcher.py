import base64
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime
from io import BytesIO

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

from .const import BALANCE_URL, ELECTRIC_USAGE_URL, LOGIN_URL
from .error_watcher import ErrorWatcher
from .onnx import ONNX
from .sensor_updator import SensorUpdator


def base64_to_PLI(base64_str: str):
    base64_data = re.sub("^data:image/.+;base64,", "", base64_str)
    byte_data = base64.b64decode(base64_data)
    image_data = BytesIO(byte_data)
    img = Image.open(image_data)
    return img


def get_transparency_location(image):
    """获取基于透明元素裁切图片的左上角、右下角坐标

    :param image: cv2加载好的图像
    :return: (left, upper, right, lower)元组
    """
    # 1. 扫描获得最左边透明点和最右边透明点坐标
    height, width, channel = image.shape  # 高、宽、通道数
    assert channel == 4  # 无透明通道报错
    first_location = None  # 最先遇到的透明点
    last_location = None  # 最后遇到的透明点
    first_transparency = []  # 从左往右最先遇到的透明点，元素个数小于等于图像高度
    last_transparency = []  # 从左往右最后遇到的透明点，元素个数小于等于图像高度
    for y, rows in enumerate(image):
        for x, BGRA in enumerate(rows):
            alpha = BGRA[3]
            if alpha != 0:
                if (
                    not first_location or first_location[1] != y
                ):  # 透明点未赋值或为同一列
                    first_location = (x, y)  # 更新最先遇到的透明点
                    first_transparency.append(first_location)
                last_location = (x, y)  # 更新最后遇到的透明点
        if last_location:
            last_transparency.append(last_location)

    # 2. 矩形四个边的中点
    top = first_transparency[0]
    bottom = first_transparency[-1]
    left = None
    right = None
    for first, last in zip(first_transparency, last_transparency):
        if not left:
            left = first
        if not right:
            right = last
        if first[0] < left[0]:
            left = first
        if last[0] > right[0]:
            right = last

    # 3. 左上角、右下角
    upper_left = (left[0], top[1])  # 左上角
    bottom_right = (right[0], bottom[1])  # 右下角

    return upper_left[0], upper_left[1], bottom_right[0], bottom_right[1]


class DataFetcher:
    def __init__(self, username: str, password: str):
        if "PYTHON_IN_DOCKER" not in os.environ:
            import dotenv

            dotenv.load_dotenv(verbose=True)
        self._username = username
        self._password = password
        self.onnx = ONNX(os.path.join(os.path.dirname(__file__), "captcha.onnx"))

        # 获取 ENABLE_DATABASE_STORAGE 的值，默认为 False
        self.enable_database_storage = (
            os.getenv("ENABLE_DATABASE_STORAGE", "false").lower() == "true"
        )
        self.RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))
        self.LOGIN_EXPECTED_TIME = int(os.getenv("LOGIN_EXPECTED_TIME", 10))
        self.RETRY_WAIT_TIME_OFFSET_UNIT = int(
            os.getenv("RETRY_WAIT_TIME_OFFSET_UNIT", 10)
        )
        self.IGNORE_USER_ID = os.getenv("IGNORE_USER_ID", "xxxxx,xxxxx").split(",")

    # @staticmethod
    def _is_captcha_legal(self, captcha):
        """check the ddddocr result, justify whether it's legal"""
        if len(captcha) != 4:
            return False
        for s in captcha:
            if not s.isalpha() and not s.isdigit():
                return False
        return True

    # @staticmethod
    def _sliding_track(self, page, distance):  # 机器模拟人工滑动轨迹
        # 获取按钮
        slider = page.locator(".slide-verify-slider-mask-item")
        box = slider.bounding_box()
        if box:
            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2

            page.mouse.move(start_x, start_y)
            page.mouse.down()

            yoffset_random = random.uniform(-2, 4)
            # 移动距离需要加上起始坐标
            target_x = start_x + distance
            target_y = start_y + yoffset_random

            page.mouse.move(target_x, target_y)
            # time.sleep(0.2)
            page.mouse.up()

    def connect_user_db(self, user_id):
        """创建数据库集合，db_name = electricity_daily_usage_{user_id}
        :param user_id: 用户ID"""
        try:
            # 创建数据库
            DB_NAME = os.getenv("DB_NAME", "homeassistant.db")
            if "PYTHON_IN_DOCKER" in os.environ:
                if os.path.exists("/data"):
                    DB_NAME = "/data/" + DB_NAME
                else:
                    DB_NAME = "data/" + DB_NAME
            self.connect = sqlite3.connect(DB_NAME)
            self.connect.cursor()
            logging.info(f"Database of {DB_NAME} created successfully.")
            # 创建表名
            self.table_name = f"daily{user_id}"
            sql = f"""CREATE TABLE IF NOT EXISTS {self.table_name} (
                    date DATE PRIMARY KEY NOT NULL, 
                    usage REAL NOT NULL)"""
            self.connect.execute(sql)
            logging.info(f"Table {self.table_name} created successfully")

            # 创建data表名
            self.table_expand_name = f"data{user_id}"
            sql = f"""CREATE TABLE IF NOT EXISTS {self.table_expand_name} (
                    name TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL)"""
            self.connect.execute(sql)
            logging.info(f"Table {self.table_expand_name} created successfully")

        # 如果表已存在，则不会创建
        except sqlite3.Error as e:
            logging.debug(f"Create db or Table error:{e}")
            return False
        return True

    def insert_data(self, data: dict):
        if self.connect is None:
            logging.error("Database connection is not established.")
            return
        # 创建索引
        try:
            sql = f"INSERT OR REPLACE INTO {self.table_name} VALUES(strftime('%Y-%m-%d','{data['date']}'),{data['usage']});"
            self.connect.execute(sql)
            self.connect.commit()
        except BaseException as e:
            logging.debug(f"Data update failed: {e}")

    def insert_expand_data(self, data: dict):
        if self.connect is None:
            logging.error("Database connection is not established.")
            return
        # 创建索引
        try:
            sql = f"INSERT OR REPLACE INTO {self.table_expand_name} VALUES('{data['name']}','{data['value']}');"
            self.connect.execute(sql)
            self.connect.commit()
        except BaseException as e:
            logging.debug(f"Data update failed: {e}")

    @ErrorWatcher.watch
    def _login(self, page, phone_code=False):
        try:
            page.goto(LOGIN_URL)
            page.wait_for_selector(".user")
        except Exception:
            logging.debug(f"Login failed, open URL: {LOGIN_URL} failed.")
        logging.info(f"Open LOGIN_URL:{LOGIN_URL}.\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
        # swtich to username-password login page
        page.locator(".user").click()
        logging.info("find_element 'user'.\r")
        page.click('xpath=//*[@id="login_box"]/div[1]/div[1]/div[2]/span')
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # click agree button
        page.click(
            'xpath=//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]'
        )
        logging.info("Click the Agree option.\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        if phone_code:
            page.click('xpath=//*[@id="login_box"]/div[1]/div[1]/div[3]/span')
            input_elements = page.locator(".el-input__inner")
            input_elements.nth(2).fill(self._username)
            logging.info(f"input_elements username : {self._username}\r")
            page.click(
                'xpath=//*[@id="login_box"]/div[2]/div[2]/form/div[1]/div[2]/div[2]/div/a'
            )
            code = input("Input your phone verification code: ")
            input_elements.nth(3).fill(code)
            logging.info(f"input_elements verification code: {code}.\r")
            # click login button
            page.click(
                'xpath=//*[@id="login_box"]/div[2]/div[2]/form/div[2]/div/button/span'
            )
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            logging.info("Click login button.\r")

            return True
        else:
            # input username and password
            input_elements = page.locator(".el-input__inner")
            input_elements.nth(0).fill(self._username)
            logging.info(f"input_elements username : {self._username}\r")
            input_elements.nth(1).fill(self._password)
            logging.info(f"input_elements password : {self._password}\r")

            # click login button
            page.click(".el-button.el-button--primary")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            logging.info("Click login button.\r")
            # sometimes ddddOCR may fail, so add retry logic)
            for retry_times in range(1, self.RETRY_TIMES_LIMIT + 1):
                page.click('xpath=//*[@id="login_box"]/div[1]/div[1]/div[2]/span')
                # get canvas image
                background_JS = 'return document.getElementById("slideVerify").childNodes[0].toDataURL("image/png");'
                # targe_JS = 'return document.getElementsByClassName("slide-verify-block")[0].toDataURL("image/png");'
                # get base64 image data
                im_info = page.evaluate(background_JS)
                background = im_info.split(",")[1]
                background_image = base64_to_PLI(background)
                logging.info("Get electricity canvas image successfully.\r")
                distance = self.onnx.get_distance(background_image)
                logging.info(f"Image CaptCHA distance is {distance}.\r")

                self._sliding_track(page, round(distance * 1.06))  # 1.06是补偿
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                if page.url == LOGIN_URL:  # if login not success
                    try:
                        logging.info(
                            "Sliding CAPTCHA recognition failed and reloaded.\r"
                        )
                        page.click(".el-button.el-button--primary")
                        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
                        continue
                    except Exception:
                        logging.debug(
                            f"Login failed, maybe caused by invalid captcha, {self.RETRY_TIMES_LIMIT - retry_times} retry times left."
                        )
                else:
                    return True
            logging.error(
                "Login failed, maybe caused by Sliding CAPTCHA recognition failed"
            )
        return False

        raise Exception(
            "Login failed, maybe caused by 1.incorrect phone_number and password, please double check. or 2. network, please mnodify LOGIN_EXPECTED_TIME in .env and run docker compose up --build."
        )

    def fetch(self):
        """main logic here"""
        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(viewport={"width": 1920, "height": 1080})
            page = context.new_page()

            ErrorWatcher.instance().set_driver(page)

            # driver.maximize_window() # viewport handles this
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            logging.info("Playwright initialized.")
            updator = SensorUpdator()

            try:
                if os.getenv("DEBUG_MODE", "false").lower() == "true":
                    if self._login(page, phone_code=True):
                        logging.info("login successed !")
                    else:
                        logging.info("login unsuccessed !")
                        raise Exception("login unsuccessed")
                else:
                    if self._login(page):
                        logging.info("login successed !")
                    else:
                        logging.info("login unsuccessed !")
                        raise Exception("login unsuccessed")
            except Exception as e:
                logging.error(
                    f"Browser quit abnormly, reason: {e}. {self.RETRY_TIMES_LIMIT} retry times left."
                )
                browser.close()
                return

            logging.info(f"Login successfully on {LOGIN_URL}")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            logging.info("Try to get the userid list")
            user_id_list = self._get_user_ids(page)
            logging.info(
                f"Here are a total of {len(user_id_list)} userids, which are {user_id_list} among which {self.IGNORE_USER_ID} will be ignored."
            )
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

            for userid_index, user_id in enumerate(user_id_list):
                try:
                    # switch to electricity charge balance page
                    page.goto(BALANCE_URL)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    self._choose_current_userid(page, userid_index)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    current_userid = self._get_current_userid(page)
                    if current_userid in self.IGNORE_USER_ID:
                        logging.info(
                            f"The user ID {current_userid} will be ignored in user_id_list"
                        )
                        continue
                    else:
                        ### get data
                        (
                            balance,
                            last_daily_date,
                            last_daily_usage,
                            yearly_charge,
                            yearly_usage,
                            month_charge,
                            month_usage,
                        ) = self._get_all_data(page, user_id, userid_index)
                        updator.update_one_userid(
                            user_id,
                            balance,
                            last_daily_date,
                            last_daily_usage,
                            yearly_charge,
                            yearly_usage,
                            month_charge,
                            month_usage,
                        )

                        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                except Exception as e:
                    if userid_index != len(user_id_list):
                        logging.info(
                            f"The current user {user_id} data fetching failed {e}, the next user data will be fetched."
                        )
                    else:
                        logging.info(f"The user {user_id} data fetching failed, {e}")
                        logging.info("Browser quit after fetching data successfully.")
                    continue

            browser.close()

    def _get_current_userid(self, page):
        current_userid = page.locator(
            'xpath=//*[@id="app"]/div/div/article/div/div/div[2]/div/div/div[1]/div[2]/div/div/div/div[2]/div/div[1]/div/ul/div/li[1]/span[2]'
        ).inner_text()
        return current_userid

    def _choose_current_userid(self, page, userid_index):
        elements = page.locator(".button_confirm").all()
        if elements:
            page.click(
                """xpath=//*[@id="app"]/div/div[2]/div/div/div/div[2]/div[2]/div/button"""
            )
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        page.click(".el-input__suffix")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        page.click(
            f"xpath=/html/body/div[2]/div[1]/div[1]/ul/li[{userid_index + 1}]/span"
        )

    def _get_all_data(self, page, user_id, userid_index):
        balance = self._get_electric_balance(page)
        if balance is None:
            logging.info(f"Get electricity charge balance for {user_id} failed, Pass.")
        else:
            logging.info(
                f"Get electricity charge balance for {user_id} successfully, balance is {balance} CNY."
            )
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # swithc to electricity usage page
        page.goto(ELECTRIC_USAGE_URL)
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        self._choose_current_userid(page, userid_index)
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # get data for each user id
        yearly_usage, yearly_charge = self._get_yearly_data(page)

        if yearly_usage is None:
            logging.error(f"Get year power usage for {user_id} failed, pass")
        else:
            logging.info(
                f"Get year power usage for {user_id} successfully, usage is {yearly_usage} kwh"
            )
        if yearly_charge is None:
            logging.error(f"Get year power charge for {user_id} failed, pass")
        else:
            logging.info(
                f"Get year power charge for {user_id} successfully, yealrly charge is {yearly_charge} CNY"
            )

        # 按月获取数据
        month, month_usage, month_charge = self._get_month_usage(page)
        if month is None:
            logging.error(f"Get month power usage for {user_id} failed, pass")
        else:
            for m in range(len(month)):
                logging.info(
                    f"Get month power charge for {user_id} successfully, {month[m]} usage is {month_usage[m]} KWh, charge is {month_charge[m]} CNY."
                )
        # get yesterday usage
        last_daily_date, last_daily_usage = self._get_yesterday_usage(page)
        if last_daily_usage is None:
            logging.error(f"Get daily power consumption for {user_id} failed, pass")
        else:
            logging.info(
                f"Get daily power consumption for {user_id} successfully, , {last_daily_date} usage is {last_daily_usage} kwh."
            )
        if month is None:
            logging.error(f"Get month power usage for {user_id} failed, pass")

        # 新增储存用电量
        if self.enable_database_storage:
            # 将数据存储到数据库
            logging.info(
                "enable_database_storage is true, we will store the data to the database."
            )
            # 按天获取数据 7天/30天
            date, usages = self._get_daily_usage_data(page)
            self._save_user_data(
                user_id,
                balance,
                last_daily_date,
                last_daily_usage,
                date,
                usages,
                month,
                month_usage,
                month_charge,
                yearly_charge,
                yearly_usage,
            )
        else:
            logging.info(
                "enable_database_storage is false, we will not store the data to the database."
            )

        if month_charge:
            month_charge = month_charge[-1]
        else:
            month_charge = None
        if month_usage:
            month_usage = month_usage[-1]
        else:
            month_usage = None

        return (
            balance,
            last_daily_date,
            last_daily_usage,
            yearly_charge,
            yearly_usage,
            month_charge,
            month_usage,
        )

    def _get_user_ids(self, page):
        try:
            # 刷新网页
            page.reload()
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            page.wait_for_selector(".el-dropdown")
            # click roll down button for user id
            page.click("xpath=//div[@class='el-dropdown']/span")
            logging.debug("""page.click("xpath=//div[@class='el-dropdown']/span")""")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # wait for roll down menu displayed
            target = page.locator(".el-dropdown-menu.el-popper").locator("li").first
            logging.debug(
                """target = page.locator(".el-dropdown-menu.el-popper").locator("li").first"""
            )
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            target.wait_for(state="visible")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            logging.debug("""target.wait_for(state="visible")""")
            # Wait for text ":"
            # WebDriverWait(driver, ...).until(EC.text_to_be_present_in_element(...))
            # In Playwright we can just wait.

            # get user id one by one
            userid_elements = (
                page.locator(".el-dropdown-menu.el-popper").locator("li").all()
            )
            userid_list = []
            for element in userid_elements:
                userid_list.append(re.findall("[0-9]+", element.inner_text())[-1])
            return userid_list
        except Exception as e:
            logging.error(
                f"Browser quit abnormly, reason: {e}. get user_id list failed."
            )
            page.close()  # Actually we should probably raise so fetch closes browser.

    def _get_electric_balance(self, page):
        try:
            balance = page.locator(".num").inner_text()
            balance_text = page.locator(".amttxt").inner_text()
            if "欠费" in balance_text:
                return -float(balance)
            else:
                return float(balance)
        except Exception:
            return None

    def _get_yearly_data(self, page):
        try:
            if datetime.now().month == 1:
                page.click(
                    'xpath=//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input'
                )
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                page.click(
                    f"xpath=//span[contains(text(), '{datetime.now().year - 1}')]"
                )
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            page.click("xpath=//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # wait for data displayed
            page.locator(".total").wait_for(state="visible")
        except Exception as e:
            logging.error(f"The yearly data get failed : {e}")
            return None, None

        # get data
        try:
            yearly_usage = page.locator(
                "xpath=//ul[@class='total']/li[1]/span"
            ).inner_text()
        except Exception as e:
            logging.error(f"The yearly_usage data get failed : {e}")
            yearly_usage = None

        try:
            yearly_charge = page.locator(
                "xpath=//ul[@class='total']/li[2]/span"
            ).inner_text()
        except Exception as e:
            logging.error(f"The yearly_charge data get failed : {e}")
            yearly_charge = None

        return yearly_usage, yearly_charge

    def _get_yesterday_usage(self, page):
        """获取最近一次用电量"""
        try:
            # 点击日用电量
            page.click(
                "xpath=//div[@class='el-tabs__nav is-top']/div[@id='tab-second']"
            )
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # wait for data displayed
            usage_element = page.locator(
                "xpath=//div[@class='el-tab-pane dayd']//div[@class='el-table__body-wrapper is-scrolling-none']/table/tbody/tr[1]/td[2]/div"
            )
            usage_element.wait_for(state="visible")  # 等待用电量出现

            # 增加是哪一天
            date_element = page.locator(
                "xpath=//div[@class='el-tab-pane dayd']//div[@class='el-table__body-wrapper is-scrolling-none']/table/tbody/tr[1]/td[1]/div"
            )
            last_daily_date = date_element.inner_text()  # 获取最近一次用电量的日期
            return last_daily_date, float(usage_element.inner_text())
        except Exception as e:
            logging.error(f"The yesterday data get failed : {e}")
            return None

    def _get_month_usage(self, page):
        """获取每月用电量"""

        try:
            page.click("xpath=//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            if datetime.now().month == 1:
                page.click(
                    'xpath=//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input'
                )
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                page.click(
                    f"xpath=//span[contains(text(), '{datetime.now().year - 1}')]"
                )
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # wait for month displayed
            page.locator(".total").wait_for(state="visible")
            month_element = page.locator(
                "xpath=//*[@id='pane-first']/div[1]/div[2]/div[2]/div/div[3]/table/tbody"
            ).inner_text()
            month_element = month_element.split("\n")
            month_element.remove("MAX")
            month_element = np.array(month_element).reshape(-1, 3)
            # 将每月的用电量保存为List
            month = []
            usage = []
            charge = []
            for i in range(len(month_element)):
                month.append(month_element[i][0])
                usage.append(month_element[i][1])
                charge.append(month_element[i][2])
            return month, usage, charge
        except Exception as e:
            logging.error(f"The month data get failed : {e}")
            return None, None, None

    # 增加获取每日用电量的函数
    def _get_daily_usage_data(self, page):
        """储存指定天数的用电量"""
        retention_days = int(os.getenv("DATA_RETENTION_DAYS", 7))  # 默认值为7天
        page.click("xpath=//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

        # 7 天在第一个 label, 30 天 开通了智能缴费之后才会出现在第二个, (sb sgcc)
        if retention_days == 7:
            page.click("xpath=//*[@id='pane-second']/div[1]/div/label[1]/span[1]")
        elif retention_days == 30:
            page.click("xpath=//*[@id='pane-second']/div[1]/div/label[2]/span[1]")
        else:
            logging.error(f"Unsupported retention days value: {retention_days}")
            return

        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

        # 等待用电量的数据出现
        usage_element = page.locator(
            "xpath=//div[@class='el-tab-pane dayd']//div[@class='el-table__body-wrapper is-scrolling-none']/table/tbody/tr[1]/td[2]/div"
        )
        usage_element.wait_for(state="visible")

        # 获取用电量的数据
        days_element = page.locator(
            "xpath=//*[@id='pane-second']/div[2]/div[2]/div[1]/div[3]/table/tbody/tr"
        ).all()  # 用电量值列表
        date = []
        usages = []
        # 将用电量保存为字典
        for i in days_element:
            day = i.locator("xpath=td[1]/div").inner_text()
            usage = i.locator("xpath=td[2]/div").inner_text()
            if usage != "":
                usages.append(usage)
                date.append(day)
            else:
                logging.info(f"The electricity consumption of {usage} get nothing")
        return date, usages

    def _save_user_data(
        self,
        user_id,
        balance,
        last_daily_date,
        last_daily_usage,
        date,
        usages,
        month,
        month_usage,
        month_charge,
        yearly_charge,
        yearly_usage,
    ):
        # 连接数据库集合
        if self.connect_user_db(user_id):
            # 写入当前户号
            dic = {"name": "user", "value": f"{user_id}"}
            self.insert_expand_data(dic)
            # 写入剩余金额
            dic = {"name": "balance", "value": f"{balance}"}
            self.insert_expand_data(dic)
            # 写入最近一次更新时间
            dic = {"name": "daily_date", "value": f"{last_daily_date}"}
            self.insert_expand_data(dic)
            # 写入最近一次更新时间用电量
            dic = {"name": "daily_usage", "value": f"{last_daily_usage}"}
            self.insert_expand_data(dic)

            # 写入年用电量
            dic = {"name": "yearly_usage", "value": f"{yearly_usage}"}
            self.insert_expand_data(dic)
            # 写入年用电电费
            dic = {"name": "yearly_charge", "value": f"{yearly_charge} "}
            self.insert_expand_data(dic)

            for index in range(len(date)):
                dic = {"date": date[index], "usage": float(usages[index])}
                # 插入到数据库
                try:
                    self.insert_data(dic)
                    logging.info(
                        f"The electricity consumption of {usages[index]}KWh on {date[index]} has been successfully deposited into the database"
                    )
                except Exception as e:
                    logging.debug(
                        f"The electricity consumption of {date[index]} failed to save to the database, which may already exist: {str(e)}"
                    )

            for index in range(len(month)):
                try:
                    dic = {
                        "name": f"{month[index]}usage",
                        "value": f"{month_usage[index]}",
                    }
                    self.insert_expand_data(dic)
                    dic = {
                        "name": f"{month[index]}charge",
                        "value": f"{month_charge[index]}",
                    }
                    self.insert_expand_data(dic)
                except Exception as e:
                    logging.debug(
                        f"The electricity consumption of {month[index]} failed to save to the database, which may already exist: {str(e)}"
                    )
            if month_charge:
                month_charge = month_charge[-1]
            else:
                month_charge = None

            if month_usage:
                month_usage = month_usage[-1]
            else:
                month_usage = None
            # 写入本月电量
            dic = {"name": "month_usage", "value": f"{month_usage}"}
            self.insert_expand_data(dic)
            # 写入本月电费
            dic = {"name": "month_charge", "value": f"{month_charge}"}
            self.insert_expand_data(dic)
            # dic = {'date': month[index], 'usage': float(month_usage[index]), 'charge': float(month_charge[index])}
            self.connect.close()
        else:
            logging.info(
                "The database creation failed and the data was not written correctly."
            )
            return


if __name__ == "__main__":
    with open("bg.jpg", "rb") as f:
        test1 = f.read()
        print(type(test1))
        print(test1)
