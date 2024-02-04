from __future__ import annotations

import re
from contextlib import suppress
from typing import Optional, Union

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import FrameLocator, Page, Request, TimeoutError

from recognizer import Detector


class AsyncChallenger:
    def __init__(self, page: Page, click_timeout: Optional[int] = None, retry_times: int = 15) -> None:
        """
        Initialize a reCognizer AsyncChallenger instance with specified configurations.

        Args:
            page (Page): The Playwright Page to initialize on.
            click_timeout (int, optional): Click Timeouts between captcha-clicks.
            retry_times (int, optional): Maximum amount of retries before raising an Exception. Defaults to 15.
        """
        self.page = page
        self.detector = Detector()

        self.click_timeout = click_timeout
        self.retry_times = retry_times
        self.retried = 0
        self.dynamic: bool = False
        self.captcha_token: str = ""

        self.page.on("request", self.request_handler)

    async def request_handler(self, request: Request) -> None:
        # Check if Request url matches wanted
        if ("google.com" not in request.url and "recaptcha.net" not in request.url) or ("reload" not in request.url and "userverify" not in request.url):
            return

        with suppress(Exception):
            response = await request.response()
            assert response
            response_text = await response.text()

            self.dynamic = "dynamic" in response_text

            # Checking if captcha succeeded
            if "userverify" in request.url and "rresp" not in response_text and "bgdata" not in response_text:
                match = re.search(r'"uvresp"\s*,\s*"([^"]+)"', response_text)
                assert match
                self.captcha_token = match.group(1)

    async def check_result(self) -> Union[str, None]:
        with suppress(PlaywrightError):
            captcha_token: str = await self.page.evaluate("grecaptcha.getResponse()")
            return captcha_token

        with suppress(PlaywrightError):
            enterprise_captcha_token: str = await self.page.evaluate("grecaptcha.enterprise.getResponse()")
            return enterprise_captcha_token

        return None

    async def check_captcha_visible(self):
        captcha_frame = self.page.frame_locator("//iframe[contains(@src,'bframe')]")
        label_obj = captcha_frame.locator("//strong")
        try:
            await label_obj.wait_for(state="visible", timeout=10000)
            return True
        except TimeoutError:
            return False

    async def click_checkbox(self) -> bool:
        # Clicking Captcha Checkbox
        try:
            checkbox = self.page.frame_locator("iframe[title='reCAPTCHA']")
            await checkbox.locator(".recaptcha-checkbox-border").click(timeout=5000)
            return True
        except TimeoutError:
            return False

    async def detect_tiles(self, prompt: str, area_captcha: bool, verify: bool, captcha_frame: FrameLocator) -> Union[str, bool]:
        image = await self.page.screenshot()
        response, coordinates = self.detector.detect(prompt, image, area_captcha=area_captcha)

        if not any(response):
            if not verify:
                print("[ERROR] Detector did not return any Results.")
                return await self.retry(captcha_frame, verify=verify)
            else:
                return False

        for coord_x, coord_y in coordinates:
            await self.page.mouse.click(coord_x, coord_y)
            if self.click_timeout:
                await self.page.wait_for_timeout(self.click_timeout)

        return True

    async def retry(self, captcha_frame, verify=False) -> Union[str, bool]:
        self.retried += 1
        if self.retried >= self.retry_times:
            raise RecursionError(f"Exceeded maximum retry times of {self.retry_times}")

        # Resetting Values
        self.dynamic = False
        self.captcha_token = ""

        # Clicking Reload Button
        if verify:
            reload_button = captcha_frame.locator("#recaptcha-verify-button")
        else:
            print("[INFO] Reloading Captcha and Retrying")
            reload_button = captcha_frame.locator("#recaptcha-reload-button")
        await reload_button.click()
        return await self.handle_recaptcha()

    async def handle_recaptcha(self) -> Union[str, bool]:
        try:
            # Getting the Captcha Frame
            captcha_frame = self.page.frame_locator("//iframe[contains(@src,'bframe')]")
            label_obj = captcha_frame.locator("//strong")
            prompt = await label_obj.text_content()

            if not prompt:
                raise ValueError("reCaptcha Task Text did not load.")

        except TimeoutError:
            # Checking if Captcha Token is available
            if captcha_token := await self.check_result():
                return captcha_token

            print("[ERROR] reCaptcha Frame did not load.")
            return False

        # Getting Recaptcha Tiles
        recaptcha_tiles = await captcha_frame.locator("[class='rc-imageselect-tile']").all()
        # Checking if Captcha Loaded Properly
        for _ in range(10):
            if len(recaptcha_tiles) in (9, 16):
                break

            await self.page.wait_for_timeout(1000)
            recaptcha_tiles = await captcha_frame.locator("[class='rc-imageselect-tile']").all()

        # Detecting Images and Clicking right Coordinates
        area_captcha = len(recaptcha_tiles) == 16
        not_yet_passed = await self.detect_tiles(prompt, area_captcha, area_captcha, captcha_frame)

        if self.dynamic and not area_captcha:
            while not_yet_passed:
                await self.page.wait_for_timeout(5000)
                not_yet_passed = await self.detect_tiles(prompt, area_captcha, True, captcha_frame)

        # Check if returned captcha_token (str)
        if isinstance(not_yet_passed, str):
            return not_yet_passed

        # Resetting value if challenge fails
        # Submit challenge
        try:
            submit_button = captcha_frame.locator("#recaptcha-verify-button")
            await submit_button.click(timeout=5000)
        except TimeoutError:
            if await self.check_captcha_visible():
                print("[WARNING] Could not submit challenge. Verify Button did not load.")

        # Waiting for captcha_token for 5 seconds
        for _ in range(5):
            if (captcha_token := self.captcha_token) or (captcha_token := await self.check_result()):
                return captcha_token

            await self.page.wait_for_timeout(1000)

        # Retrying
        self.retried += 1
        if self.retried >= self.retry_times:
            raise RecursionError(f"Exceeded maximum retry times of {self.retry_times}")

        return await self.handle_recaptcha()

    async def solve_recaptcha(self) -> Union[str, bool]:
        """
        Solve a hcaptcha-challenge on the specified Playwright Page

        Returns:
            str/bool: The result of the challenge
        Raises:
            RecursionError: If the challenger doesn´t succeed in the given retry times
        """
        # Resetting Values
        self.dynamic = False
        self.captcha_token = ""

        await self.click_checkbox()
        if not await self.check_captcha_visible():
            print("[ERROR] reCaptcha Challenge is not visible.")
            return False

        await self.page.wait_for_timeout(2000)
        return await self.handle_recaptcha()
