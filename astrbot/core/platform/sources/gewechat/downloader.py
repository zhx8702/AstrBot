from astrbot import logger
import xml.etree.ElementTree as ET
import aiohttp
import json


class GeweDownloader:
    def __init__(self, base_url: str, download_base_url: str, token: str):
        self.base_url = base_url
        self.download_base_url = download_base_url
        self.headers = {"Content-Type": "application/json", "X-GEWE-TOKEN": token}

    async def _post_json(self, baseurl: str, route: str, payload: dict):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{baseurl}{route}", headers=self.headers, json=payload
            ) as resp:
                return await resp.read()

    async def download_voice(self, appid: str, xml: str, msg_id: str):
        payload = {"appId": appid, "xml": xml, "msgId": msg_id}
        return await self._post_json(self.base_url, "/message/downloadVoice", payload)

    async def download_image(self, wxid: str, content: str) -> str:
        """返回一个可下载的 URL"""
        # 解析图片消息
        aeskey, cdnmidimgurl = None, None
        try:
            root = ET.fromstring(content)
            img_element = root.find("img")
            if img_element is not None:
                aeskey = img_element.get("aeskey")
                cdnmidimgurl = img_element.get("cdnmidimgurl")
        except Exception as e:
            logger.error("解析图片消息失败: {}", e)
            return

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": wxid, "AesKey": aeskey, "Cdnmidimgurl": cdnmidimgurl}
            response = await session.post(
                f"{self.base_url}/CdnDownloadImg", json=json_param
            )
            json_resp = await response.json()

            if json_resp.get("Success"):
                return json_resp.get("Data")
            else:
                print(json_resp)
                raise Exception(f"下载图片失败: {json_resp.get('Message')}")

    async def download_emoji_md5(self, app_id, emoji_md5):
        """下载emoji"""
        try:
            payload = {"appId": app_id, "emojiMd5": emoji_md5}

            # gewe 计划中的接口，暂时没有实现。返回代码404
            data = await self._post_json(
                self.base_url, "/message/downloadEmojiMd5", payload
            )
            json_blob = json.loads(data)
            return json_blob
        except BaseException as e:
            logger.error(f"gewe download emoji: {e}")
