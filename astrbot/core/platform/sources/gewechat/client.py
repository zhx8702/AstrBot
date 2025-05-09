import asyncio
import base64
import string
import datetime
import os
import re
import uuid
from random import choice
from typing import Union
import hashlib

import aiohttp
import anyio
import quart

from astrbot.api import logger, sp
from astrbot.api.message_components import Plain, Image, At, Record, Video
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from .downloader import GeweDownloader

try:
    from .xml_data_parser import GeweDataParser
except (ImportError, ModuleNotFoundError) as e:
    logger.warning(
        f"警告: 可能未安装 defusedxml 依赖库，将导致无法解析微信的 表情包、引用 类型的消息: {str(e)}"
    )


class SimpleGewechatClient:
    """针对 Gewechat 的简单实现。

    @author: Soulter
    @website: https://github.com/Soulter
    """

    def __init__(
        self,
        base_url: str,
        nickname: str,
        host: str,
        port: int,
        event_queue: asyncio.Queue,
    ):
        self.base_url = base_url
        if self.base_url.endswith("/"):
            self.base_url = self.base_url[:-1]

        self.download_base_url = self.base_url.split(":")[:-1]  # 去掉端口
        self.download_base_url = ":".join(self.download_base_url) + ":2532/download/"

        logger.info(f"Gewechat API: {self.base_url}")
        logger.info(f"Gewechat 下载 API: {self.download_base_url}")

        if isinstance(port, str):
            port = int(port)

        self.token = None
        self.headers = {}
        self.nickname = nickname
        self.wxid = None
        self.device_id = None
        self.device_name = None
        self.uuid = None
        self.sync_key = ""  # 用于消息同步的key

        # 尝试从SharedPreferences加载已保存的登录信息
        saved_info = sp.get(f"gewechat-info-{self.nickname}", None)
        if saved_info:
            logger.info(f"从存储中加载 gewechat 账号 {self.nickname} 的登录信息")
            if "wxid" in saved_info:
                self.wxid = saved_info["wxid"]
            if "device_name" in saved_info:
                self.device_name = saved_info["device_name"]
            if "device_id" in saved_info:
                self.device_id = saved_info["device_id"]
            if "uuid" in saved_info:
                self.uuid = saved_info["uuid"]
        logger.info(f"Gewechat 账号 {self.nickname} 的登录信息: {saved_info}")

        self.server = quart.Quart(__name__)
        self.server.add_url_rule(
            "/astrbot-gewechat/file/<file_token>",
            view_func=self._handle_file,
            methods=["GET"],
        )

        self.host = host
        self.port = port
        self.callback_url = f"http://{self.host}:{self.port}/astrbot-gewechat/callback"
        self.file_server_url = f"http://{self.host}:{self.port}/astrbot-gewechat/file"

        self.event_queue = event_queue

        self.multimedia_downloader = None

        self.userrealnames = {}

        self.shutdown_event = asyncio.Event()

        self.staged_files = {}
        """存储了允许外部访问的文件列表。auth_token: file_path。通过 register_file 方法注册。"""

        self.lock = asyncio.Lock()

    async def _convert(self, data: dict) -> AstrBotMessage:
        # if "TypeName" in data:
        #     type_name = data["TypeName"]
        # elif "type_name" in data:
        #     type_name = data["type_name"]
        # else:
        #     raise Exception("无法识别的消息类型")

        # # 以下没有业务处理，只是避免控制台打印太多的日志
        # if type_name == "ModContacts":
        #     logger.info("gewechat下发：ModContacts消息通知。")
        #     return
        # if type_name == "DelContacts":
        #     logger.info("gewechat下发：DelContacts消息通知。")
        #     return

        # if type_name == "Offline":
        #     logger.critical("收到 gewechat 下线通知。")
        #     return

        d = data
        # if "Data" in data:
        #     d = data["Data"]
        # elif "data" in data:
        #     d = data["data"]

        # if not d:
        #     logger.warning(f"消息不含 data 字段: {data}")
        #     return

        if "CreateTime" in d:
            # 得到系统 UTF+8 的 ts
            tz_offset = datetime.timedelta(hours=8)
            tz = datetime.timezone(tz_offset)
            ts = datetime.datetime.now(tz).timestamp()
            create_time = d["CreateTime"]
            if create_time < ts - 30:
                logger.warning(f"消息时间戳过旧: {create_time}，当前时间戳: {ts}")
                return

        abm = AstrBotMessage()

        from_user_name = d["FromUserName"]["string"]  # 消息来源
        d["to_wxid"] = from_user_name  # 用于发信息

        abm.message_id = str(d.get("MsgId"))
        abm.session_id = from_user_name
        abm.self_id = self.wxid  # 机器人的 wxid

        user_id = ""  # 发送人 wxid
        content = d["Content"]["string"]  # 消息内容

        at_me = False
        at_wxids = []
        if "@chatroom" in from_user_name:
            abm.type = MessageType.GROUP_MESSAGE
            _t = content.split(":\n")
            user_id = _t[0]
            content = _t[1]
            # at
            msg_source = d["MsgSource"]
            if "\u2005" in content:
                # at
                # content = content.split('\u2005')[1]
                content = re.sub(r"@[^\u2005]*\u2005", "", content)
                at_wxids = re.findall(
                    r"<atuserlist><!\[CDATA\[.*?(?:,|\b)([^,]+?)(?=,|\]\]></atuserlist>)",
                    msg_source,
                )

            abm.group_id = from_user_name

            if (
                f"<atuserlist><![CDATA[,{abm.self_id}]]>" in msg_source
                or f"<atuserlist><![CDATA[{abm.self_id}]]>" in msg_source
            ):
                at_me = True
            if "在群聊中@了你" in d.get("PushContent", ""):
                at_me = True
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            user_id = from_user_name

        # 检查消息是否由自己发送，若是则忽略
        # 已经有可配置项专门配置是否需要响应自己的消息，因此这里注释掉。
        # if user_id == abm.self_id:
        #     logger.info("忽略自己发送的消息")
        #     return None

        abm.message = []

        # 解析用户真实名字
        user_real_name = "unknown"
        if abm.group_id:
            if (
                abm.group_id not in self.userrealnames
                or user_id not in self.userrealnames[abm.group_id]
            ):
                # 获取群成员列表，并且缓存
                if abm.group_id not in self.userrealnames:
                    self.userrealnames[abm.group_id] = {}
                member_list = await self.get_chatroom_member_list(abm.group_id)
                logger.debug(f"获取到 {abm.group_id} 的群成员列表。")
                if member_list and "memberList" in member_list:
                    for member in member_list["memberList"]:
                        self.userrealnames[abm.group_id][member["wxid"]] = member[
                            "nickName"
                        ]
                if user_id in self.userrealnames[abm.group_id]:
                    user_real_name = self.userrealnames[abm.group_id][user_id]
            else:
                user_real_name = self.userrealnames[abm.group_id][user_id]
        else:
            try:
                # info = (await self.get_user_info(user_id))
                # user_real_name = info["nickName"]
                user_real_name = ""
            except Exception as e:
                logger.debug(f"获取用户 {user_id} 昵称失败: {e}")
                user_real_name = user_id

        if at_me:
            abm.message.insert(0, At(qq=abm.self_id, name=self.nickname))
        for wxid in at_wxids:
            # 群聊里 At 其他人的列表
            _username = self.userrealnames.get(abm.group_id, {}).get(wxid, wxid)
            abm.message.append(At(qq=wxid, name=_username))

        abm.sender = MessageMember(user_id, user_real_name)
        abm.raw_message = d
        abm.message_str = ""

        if user_id == "weixin":
            # 忽略微信团队消息
            return

        # 不同消息类型
        msg_type = d["MsgType"]
        print(f"消息类型: {msg_type}")
        if msg_type == 1:
            # 文本消息
            abm.message.append(Plain(content))
            abm.message_str = content
        elif msg_type == 3:
            # 图片消息
            img_b64 = await self.multimedia_downloader.download_image(
                self.wxid, content
            )
            logger.debug(f"下载图片: {img_b64}")
            abm.message.append(Image.fromBase64(img_b64))

        elif msg_type == 34:
            # 语音消息
            if "ImgBuf" in d and "buffer" in d["ImgBuf"]:
                voice_data = base64.b64decode(d["ImgBuf"]["buffer"])
                file_path = f"data/temp/gewe_voice_{abm.message_id}.silk"

                async with await anyio.open_file(file_path, "wb") as f:
                    await f.write(voice_data)
                abm.message.append(Record(file=file_path, url=file_path))

        # 以下已知消息类型，没有业务处理，只是避免控制台打印太多的日志
        elif msg_type == 37:  # 好友申请
            logger.info("消息类型(37)：好友申请")
        elif msg_type == 42:  # 名片
            logger.info("消息类型(42)：名片")
        elif msg_type == 43:  # 视频
            video = Video(file="", cover=content)
            abm.message.append(video)
        elif msg_type == 47:  # emoji
            data_parser = GeweDataParser(content, abm.group_id == "")
            emoji = data_parser.parse_emoji()
            abm.message.append(emoji)
        elif msg_type == 48:  # 地理位置
            logger.info("消息类型(48)：地理位置")
        elif msg_type == 49:  # 公众号/文件/小程序/引用/转账/红包/视频号/群聊邀请
            data_parser = GeweDataParser(content, abm.group_id == "")
            segments = data_parser.parse_mutil_49()
            if segments:
                abm.message.extend(segments)
                for seg in segments:
                    if isinstance(seg, Plain):
                        abm.message_str += seg.text
        elif msg_type == 51:  # 帐号消息同步?
            logger.info("消息类型(51)：帐号消息同步？")
        elif msg_type == 10000:  # 被踢出群聊/更换群主/修改群名称
            logger.info("消息类型(10000)：被踢出群聊/更换群主/修改群名称")
        elif (
            msg_type == 10002
        ):  # 撤回/拍一拍/成员邀请/被移出群聊/解散群聊/群公告/群待办
            logger.info(
                "消息类型(10002)：撤回/拍一拍/成员邀请/被移出群聊/解散群聊/群公告/群待办"
            )

        else:
            logger.info(f"未实现的消息类型: {d['MsgType']}")
            abm.raw_message = d

        logger.debug(f"abm: {abm}")
        return abm

    async def _callback(self):
        data = await quart.request.json
        logger.debug(f"收到 gewechat 回调: {data}")

        if data.get("testMsg", None):
            return quart.jsonify({"r": "AstrBot ACK"})

        abm = None
        try:
            abm = await self._convert(data)
        except BaseException as e:
            logger.warning(
                f"尝试解析 GeweChat 下发的消息时遇到问题: {e}。下发消息内容: {data}。"
            )

        if abm:
            coro = getattr(self, "on_event_received")
            if coro:
                await coro(abm)

        return quart.jsonify({"r": "AstrBot ACK"})

    async def _register_file(self, file_path: str) -> str:
        """向 AstrBot 回调服务器 注册一个允许外部访问的文件。

        Args:
            file_path (str): 文件路径。
        Returns:
            str: 返回一个 auth_token，文件路径为 file_path。通过 /astrbot-gewechat/file/auth_token 得到文件。
        """
        async with self.lock:
            if not os.path.exists(file_path):
                raise Exception(f"文件不存在: {file_path}")

            file_token = str(uuid.uuid4())
            self.staged_files[file_token] = file_path
            return file_token

    async def _handle_file(self, file_token):
        async with self.lock:
            if file_token not in self.staged_files:
                logger.warning(f"请求的文件 {file_token} 不存在。")
                return quart.abort(404)
            if not os.path.exists(self.staged_files[file_token]):
                logger.warning(f"请求的文件 {self.staged_files[file_token]} 不存在。")
                return quart.abort(404)
            file_path = self.staged_files[file_token]
            self.staged_files.pop(file_token, None)
            return await quart.send_file(file_path)

    async def _set_callback_url(self):
        logger.info("设置回调，请等待...")
        await asyncio.sleep(3)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/tools/setCallback",
                headers=self.headers,
                json={"token": self.token, "callbackUrl": self.callback_url},
            ) as resp:
                json_blob = await resp.json()
                logger.info(f"设置回调结果: {json_blob}")
                if json_blob["ret"] != 200:
                    raise Exception(f"设置回调失败: {json_blob}")
                logger.info(
                    f"将在 {self.callback_url} 上接收 gewechat 下发的消息。如果一直没收到消息请先尝试重启 AstrBot。如果仍没收到请到管理面板聊天页输入 /gewe_logout 重新登录。"
                )

    async def start_polling(self):
        """启动轮询以获取消息。
        使用Sync API每0.5秒轮询一次，获取新消息。
        """
        if not self.wxid:
            logger.error("未登录，无法启动消息轮询")
            return

        is_logged = await self.is_logged_in()
        if not is_logged:
            logger.error("登录状态异常，无法启动消息轮询")
            return

        # 确保有token
        if not self.token:
            logger.warning("token未设置，尝试进行初始化...")
            # 可能还没有完全初始化
            try:
                # 尝试获取一些基本信息来初始化token
                await self.get_profile(self.wxid)
            except Exception as e:
                logger.error(f"初始化token失败: {e}")
                return

        logger.info("开始轮询消息...")

        # 创建消息处理队列
        message_queue = asyncio.Queue()

        # 启动文件服务器，用于处理文件下载请求
        server_task = asyncio.create_task(
            self.server.run_task(
                host="0.0.0.0",
                port=self.port,
                shutdown_trigger=self.shutdown_trigger,
            )
        )

        # 创建消息处理任务
        processor_task = asyncio.create_task(self._process_message_queue(message_queue))

        # 轮询消息
        try:
            while not self.shutdown_event.is_set():
                try:
                    # 执行Sync请求获取最新消息
                    messages = await self._poll_messages()
                    if messages and isinstance(messages, list):
                        # 将获取到的消息加入队列
                        for msg in messages:
                            await message_queue.put(msg)
                except Exception as e:
                    logger.error(f"轮询消息时发生错误: {e}")

                # 延迟0.5秒再次轮询
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            logger.info("轮询任务被取消")
        finally:
            # 确保所有任务都被取消
            if not processor_task.done():
                processor_task.cancel()
                try:
                    await processor_task
                except asyncio.CancelledError:
                    pass

            if not server_task.done():
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass

    async def _process_message_queue(self, queue: asyncio.Queue):
        """处理消息队列中的消息"""
        while not self.shutdown_event.is_set():
            try:
                # 从队列中获取消息
                msg = await queue.get()
                logger.debug(f"处理队列中的消息: {msg}")

                try:
                    # 将消息转换为AstrBotMessage格式
                    abm = await self._convert(msg)
                    if abm:
                        # 如果存在on_event_received方法，调用它处理消息
                        coro = getattr(self, "on_event_received", None)
                        if coro:
                            await coro(abm)
                except Exception as e:
                    logger.error(f"处理消息时出错: {e}")

                # 标记任务完成
                queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"消息队列处理器出错: {e}")

    async def _poll_messages(self):
        """执行Sync API请求以获取最新消息，返回消息列表"""
        # 检查是否已登录
        if not await self.is_logged_in():
            logger.warning("未登录状态，暂停消息同步")
            # 暂停同步，避免大量错误日志
            await asyncio.sleep(5)
            asyncio.create_task(self.login())
            # 暂停较长时间
            await asyncio.sleep(30)
            return []

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                json_param = {"Wxid": self.wxid, "Scene": 0, "Synckey": self.sync_key}
                async with session.post(
                    f"{self.base_url}/Sync", headers=self.headers, json=json_param
                ) as response:
                    json_resp = await response.json()

                    if json_resp.get("Success"):
                        messages = json_resp.get("Data", {}).get("AddMsgs", [])
                        if messages:
                            # 更新sync_key以便下次同步
                            self.sync_key = json_resp.get("Data", {}).get(
                                "SyncKey", self.sync_key
                            )
                            return messages
                        return []
        except asyncio.TimeoutError as e:
            logger.error(f"执行同步消息请求时出错 {e}")
            return []
        except Exception as e:
            logger.error(f"执行同步消息请求时出错 {e}")
            return []

    async def shutdown_trigger(self):
        await self.shutdown_event.wait()

    async def check_online(self):
        """检查 uuid 对应的设备是否在线。"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/CheckUuid",
                headers=self.headers,
                json={"Uuid": self.uuid},
            ) as resp:
                json_blob = await resp.json()
                return json_blob["Success"]

    async def check_login_uuid(self) -> tuple[bool, Union[dict, int]]:
        """检查登录的UUID状态。

        Returns:
            tuple[bool, Union[dict, int]]: 如果登录成功返回(True, 用户信息)，否则返回(False, 过期时间)

        Raises:
            根据error_handler处理错误
        """
        if not self.uuid:
            raise Exception("UUID不存在，请先获取登录二维码")

        async with aiohttp.ClientSession() as session:
            json_param = {"Uuid": self.uuid}
            try:
                response = await session.post(
                    f"{self.base_url}/CheckUuid", json=json_param
                )
                json_resp = await response.json()

                logger.debug(f"检查登录状态结果: {json_resp}")

                if json_resp.get("Success"):
                    if json_resp.get("Data", {}).get("acctSectResp", ""):
                        self.wxid = (
                            json_resp.get("Data", {})
                            .get("acctSectResp", {})
                            .get("userName")
                        )
                        # self.nickname = json_resp.get("Data", {}).get("acctSectResp", {}).get("nickName")
                        # 保存登录成功的wxid和nickname到SharedPreferences
                        info = sp.get(f"gewechat-info-{self.nickname}", {})
                        info["wxid"] = self.wxid
                        info["nickname"] = self.nickname
                        if self.device_name:
                            info["device_name"] = self.device_name
                        if self.device_id:
                            info["device_id"] = self.device_id
                        if self.uuid:
                            info["uuid"] = self.uuid
                        sp.put(f"gewechat-info-{self.nickname}", info)
                        return True, json_resp.get("Data")
                    else:
                        return False, json_resp.get("Data", {}).get("expiredTime", 0)
                else:
                    raise Exception(f"检查UUID状态失败: {json_resp.get('Message', '')}")
            except Exception as e:
                logger.error(f"检查登录状态时出错: {e}")
                raise

    async def logout(self):
        """登出 gewechat。"""
        if self.uuid:
            try:
                online = await self.check_online()
                if online:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{self.base_url}/LogOut",
                            headers=self.headers,
                            json={"Wxid": self.uuid},
                        ) as resp:
                            json_blob = await resp.json()
                            if json_blob["Success"]:
                                logger.info("登出成功")
                                # 删除保存的登录信息
                                sp.remove(f"gewechat-info-{self.nickname}")
                                logger.info(
                                    f"已删除 gewechat 账号 {self.nickname} 的保存信息"
                                )
                            else:
                                logger.warning(
                                    f"登出失败: {json_blob.get('Message', '')}"
                                )
            except Exception as e:
                logger.error(f"登出过程中出现错误: {e}")

    async def login(self):
        # 防止重复登录
        if not hasattr(self, "_login_in_progress"):
            self._login_in_progress = False

        if self._login_in_progress:
            logger.info("登录已在进行中，跳过重复登录")
            return

        try:
            self._login_in_progress = True
            # Get and cache login information
            if not await self.is_logged_in():
                while not await self.is_logged_in():
                    try:
                        if self.wxid and await self.get_cached_info():
                            # 尝试唤醒登录
                            logger.info("尝试唤醒登录")
                            self.uuid = await self.awaken_login()
                            logger.info(f"获取到登录uuid: {self.uuid}")
                        else:
                            # 二维码登录
                            logger.info("开始二维码登录流程")
                            self.uuid = await self.login_qr_code()
                            logger.info(f"二维码登录获取到UUID: {self.uuid}")
                    except Exception as e:
                        logger.error(f"登录过程中出错: {e}")
                        # 二维码登录
                        logger.info("尝试二维码登录")
                        self.uuid = await self.login_qr_code()

                    if not self.uuid:
                        logger.error("获取UUID失败，登录中断")
                        return

                    logger.info("等待用户扫描登录...")
                    try_count = 0
                    max_tries = 12  # 最多等待约1分钟
                    while try_count < max_tries:
                        try:
                            stat, data = await self.check_login_uuid()
                            if stat:
                                logger.info("登录成功")
                                break
                            logger.info(f"等待登录中，过期倒计时：{data}")
                        except Exception as e:
                            logger.error(f"检查登录状态出错: {e}")
                            break
                        await asyncio.sleep(5)
                        try_count += 1

                    if try_count >= max_tries:
                        logger.warning("等待登录超时，中断登录流程")
                        return

                # 保存登录信息到SharedPreferences
                info = {}
                info["wxid"] = self.wxid
                info["device_name"] = self.device_name
                info["device_id"] = self.device_id
                info["uuid"] = self.uuid
                sp.put(f"gewechat-info-{self.nickname}", info)
                logger.info(f"已保存 gewechat 账号 {self.nickname} 的登录信息")

                # 初始化下载器
                if not self.multimedia_downloader:
                    self.multimedia_downloader = GeweDownloader(
                        self.base_url, self.download_base_url, self.token
                    )
                    logger.info("已初始化多媒体下载器")
            else:
                logger.info("已经登录，无需重新登录")

                # 确保下载器已初始化
                if not self.multimedia_downloader:
                    self.multimedia_downloader = GeweDownloader(
                        self.base_url, self.download_base_url, self.token
                    )
                    logger.info("已初始化多媒体下载器")

            # 开启自动心跳
            try:
                success = await self.start_auto_heartbeat()
                if success:
                    logger.info("已开启自动心跳")
                else:
                    logger.warning("开启自动心跳失败")
            except ValueError:
                logger.warning("自动心跳已在运行")
            except Exception as e:
                if "在运行" not in str(e):
                    logger.warning(f"开启自动心跳时出错: {e}")
        finally:
            self._login_in_progress = False

    """API 部分。Gewechat 的 API 文档请参考: https://apifox.com/apidoc/shared/69ba62ca-cb7d-437e-85e4-6f3d3df271b1
    """

    async def get_chatroom_member_list(self, chatroom_wxid: str) -> dict:
        """获取群成员列表。

        Args:
            chatroom_wxid (str): 微信群聊的id。可以通过 event.get_group_id() 获取。

        Returns:
            dict: 返回群成员列表字典。其中键为 memberList 的值为群成员列表。
        """
        payload = {"Wxid": self.wxid, "Chatroom": chatroom_wxid}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/GetChatroomMemberDetail",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                return json_blob["Data"]

    async def post_text(self, to_wxid, content: str, ats: str = ""):
        """发送纯文本消息"""
        payload = {
            "Wxid": self.wxid,
            "ToWxid": to_wxid,
            "content": content,
            "Type": 1,
        }
        if ats:
            payload["ats"] = ats

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/SendTextMsg", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送消息结果: {json_blob}")

    async def post_image(self, to_wxid, image_url: str):
        """发送图片消息"""
        payload = {"Wxid": self.wxid, "ToWxid": to_wxid, "Base64": image_url}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/SendImageMsg", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送图片结果: {json_blob}")

    async def post_emoji(self, to_wxid, emoji_md5, emoji_size, cdnurl=""):
        """发送emoji消息"""
        payload = {
            "Wxid": self.wxid,
            "ToWxid": to_wxid,
            "Md5": emoji_md5,
            "TotalLen": emoji_size,
        }

        # 优先表情包，若拿不到表情包的md5，就用当作图片发
        try:
            if emoji_md5 != "" and emoji_size != "":
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/SendEmojiMsg",
                        headers=self.headers,
                        json=payload,
                    ) as resp:
                        json_blob = await resp.json()
                        logger.info(f"发送emoji消息结果: {json_blob.get('Success')}")
            else:
                await self.post_image(to_wxid, cdnurl)

        except Exception as e:
            logger.error(e)

    async def post_video(
        self, to_wxid, video_url: str, thumb_url: str, video_duration: int
    ):
        payload = {
            "uuid": self.uuid,
            "toWxid": to_wxid,
            "videoUrl": video_url,
            "thumbUrl": thumb_url,
            "videoDuration": video_duration,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postVideo", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送视频结果: {json_blob}")

    async def forward_video(self, to_wxid, cnd_xml: str):
        """转发视频

        Args:
            to_wxid (str): 发送给谁
            cnd_xml (str): 视频消息的cdn信息
        """
        payload = {
            "uuid": self.uuid,
            "toWxid": to_wxid,
            "xml": cnd_xml,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/forwardVideo",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"转发视频结果: {json_blob}")

    async def post_voice(self, to_wxid, voice_url: str, voice_duration: int):
        """发送语音信息

        Args:
            voice_url (str): 语音文件的网络链接
            voice_duration (int): 语音时长，毫秒
        """
        payload = {
            "uuid": self.uuid,
            "toWxid": to_wxid,
            "voiceUrl": voice_url,
            "voiceDuration": voice_duration,
        }

        logger.debug(f"发送语音: {payload}")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postVoice", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.info(f"发送语音结果: {json_blob.get('msg', '操作失败')}")

    async def post_file(self, to_wxid, file_url: str, file_name: str):
        """发送文件

        Args:
            to_wxid (string): 微信ID
            file_url (str): 文件的网络链接
            file_name (str): 文件名
        """
        payload = {
            "uuid": self.uuid,
            "toWxid": to_wxid,
            "fileUrl": file_url,
            "fileName": file_name,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/message/postFile", headers=self.headers, json=payload
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"发送文件结果: {json_blob}")

    async def add_friend(self, v3: str, v4: str, content: str):
        """申请添加好友"""
        payload = {
            "uuid": self.uuid,
            "scene": 3,
            "content": content,
            "v4": v4,
            "v3": v3,
            "option": 2,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/contacts/addContacts",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"申请添加好友结果: {json_blob}")
                return json_blob

    async def get_group(self, group_id: str):
        payload = {
            "uuid": self.uuid,
            "chatroomId": group_id,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/getChatroomInfo",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_group_member(self, group_id: str):
        payload = {
            "uuid": self.uuid,
            "chatroomId": group_id,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/getChatroomMemberList",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def accept_group_invite(self, url: str):
        """同意进群"""
        payload = {"uuid": self.uuid, "url": url}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/agreeJoinRoom",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def add_group_member_to_friend(
        self, group_id: str, to_wxid: str, content: str
    ):
        payload = {
            "uuid": self.uuid,
            "chatroomId": group_id,
            "content": content,
            "memberWxid": to_wxid,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/group/addGroupMemberAsFriend",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_user_info(self, id):
        payload = {"Wxid": id}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/GetProfile",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                # logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_user_or_group_info(self, *ids):
        """
        获取用户或群组信息。

        :param ids: 可变数量的 wxid 参数
        """

        wxids_str = list(ids)

        payload = {
            "uuid": self.uuid,
            "wxids": wxids_str,  # 使用逗号分隔的字符串
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/contacts/getDetailInfo",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取群信息结果: {json_blob}")
                return json_blob

    async def get_contacts_list(self):
        """
        获取通讯录列表
        见 https://apifox.com/apidoc/shared/69ba62ca-cb7d-437e-85e4-6f3d3df271b1/api-196794504
        """
        payload = {"uuid": self.uuid}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/contacts/fetchContactsList",
                headers=self.headers,
                json=payload,
            ) as resp:
                json_blob = await resp.json()
                logger.debug(f"获取通讯录列表结果: {json_blob}")
                return json_blob

    async def get_profile(self, wxid: str = None) -> dict:
        """获取用户信息。

        Args:
            wxid (str, optional): 用户wxid. Defaults to None.

        Returns:
            dict: 用户信息字典
        """

        if not wxid:
            wxid = self.wxid
            raise Exception("未登陆")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": wxid}
            response = await session.post(
                f"{self.base_url}/GetProfile", json=json_param
            )
            json_resp = await response.json()

            if json_resp.get("Success"):
                return json_resp.get("Data").get("userInfo")
            else:
                self.error_handler(json_resp)

    async def is_logged_in(self) -> bool:
        """检查是否登录。

        Args:
            wxid (str, optional): 用户wxid. Defaults to None.

        Returns:
            bool: 已登录返回True，未登录返回False
        """
        if not self.wxid:
            return False
        try:
            await self.get_profile(self.wxid)
            return True
        except Exception:
            is_valid = False
            logger.error("检查登录状态时出错:")
        return is_valid

    async def get_cached_info(self) -> dict:
        """获取登录缓存信息。

        Args:
            wxid (str, optional): 要查询的微信ID. Defaults to None.

        Returns:
            dict: 返回缓存信息，如果未提供wxid且未登录返回空字典
        """

        if not self.wxid:
            return {}

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(
                f"{self.base_url}/GetCachedInfo", json=json_param
            )
            json_resp = await response.json()

            if json_resp.get("Success"):
                return json_resp.get("Data")
            else:
                return {}

    async def awaken_login(self) -> str:
        """唤醒登录。

        Args:
            wxid (str, optional): 要唤醒的微信ID. Defaults to "".

        Returns:
            str: 返回新的登录UUID

        Raises:
            Exception: 如果未提供wxid且未登录
            LoginError: 如果无法获取UUID
        """
        if not self.wxid:
            raise Exception("Please login using QRCode first")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(
                f"{self.base_url}/AwakenLogin", json=json_param
            )
            json_resp = await response.json()
            logger.debug(f"唤醒登录结果: {json_resp}")

            if json_resp.get("Success") and json_resp.get("Data").get(
                "QrCodeResponse"
            ).get("Uuid"):
                uuid = json_resp.get("Data").get("QrCodeResponse").get("Uuid")
                # 保存uuid
                self.uuid = uuid
                return uuid
            elif (
                not json_resp.get("Data", {}).get("QrCodeResponse", {}).get("Uuid", "")
            ):
                raise Exception("Please login using QRCode first")
            else:
                raise Exception(str(json_resp))

    async def login_qr_code(self) -> str:
        # 如果没有设备信息，生成一个
        if not self.device_name:
            self.device_name = self.create_device_name()
        if not self.device_id:
            self.device_id = self.create_device_id()

        # Get QR code for login
        try:
            logger.info("获取登录二维码...")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/GetQRCode",
                    headers=self.headers,
                    json={"DeviceID": self.device_id, "DeviceName": self.device_name},
                ) as resp:
                    json_blob = await resp.json()
                    if not json_blob["Success"]:
                        raise Exception(f"获取二维码失败: {json_blob['Message']}")

                    qr_data = json_blob["Data"]["QRCodeBase64"]
                    qr_url = json_blob["Data"]["QRCodeURL"]
                    uuid = json_blob["Data"]["Uuid"]
                    logger.info(f"获取到UUID: {uuid}")
                    logger.warning(
                        f"请打开该网址，然后使用微信扫描二维码登录: {qr_url}"
                    )
                    if not qr_url:  # If URL is not provided, use base64 data
                        logger.warning(f"请解码并扫描二维码: {qr_data[:30]}...")

                    # 保存uuid以便后续检查登录状态
                    self.uuid = uuid

                    # 保存设备信息和uuid
                    info = sp.get(f"gewechat-info-{self.nickname}", {})
                    info["device_name"] = self.device_name
                    info["device_id"] = self.device_id
                    info["uuid"] = uuid
                    sp.put(f"gewechat-info-{self.nickname}", info)
                    logger.info(f"已保存 gewechat 设备信息到 {self.nickname}")

                    return uuid
        except Exception as e:
            raise Exception(f"获取登录二维码失败: {e}")

    @staticmethod
    def create_device_name() -> str:
        """生成一个随机的设备名。

        Returns:
            str: 返回生成的设备名
        """
        first_names = [
            "Oliver",
            "Emma",
            "Liam",
            "Ava",
            "Noah",
            "Sophia",
            "Elijah",
            "Isabella",
            "James",
            "Mia",
            "William",
            "Amelia",
            "Benjamin",
            "Harper",
            "Lucas",
            "Evelyn",
            "Henry",
            "Abigail",
            "Alexander",
            "Ella",
            "Jackson",
            "Scarlett",
            "Sebastian",
            "Grace",
            "Aiden",
            "Chloe",
            "Matthew",
            "Zoey",
            "Samuel",
            "Lily",
            "David",
            "Aria",
            "Joseph",
            "Riley",
            "Carter",
            "Nora",
            "Owen",
            "Luna",
            "Daniel",
            "Sofia",
            "Gabriel",
            "Ellie",
            "Matthew",
            "Avery",
            "Isaac",
            "Mila",
            "Leo",
            "Julian",
            "Layla",
        ]

        last_names = [
            "Smith",
            "Johnson",
            "Williams",
            "Brown",
            "Jones",
            "Garcia",
            "Miller",
            "Davis",
            "Rodriguez",
            "Martinez",
            "Hernandez",
            "Lopez",
            "Gonzalez",
            "Wilson",
            "Anderson",
            "Thomas",
            "Taylor",
            "Moore",
            "Jackson",
            "Martin",
            "Lee",
            "Perez",
            "Thompson",
            "White",
            "Harris",
            "Sanchez",
            "Clark",
            "Ramirez",
            "Lewis",
            "Robinson",
            "Walker",
            "Young",
            "Allen",
            "King",
            "Wright",
            "Scott",
            "Torres",
            "Nguyen",
            "Hill",
            "Flores",
            "Green",
            "Adams",
            "Nelson",
            "Baker",
            "Hall",
            "Rivera",
            "Campbell",
            "Mitchell",
            "Carter",
            "Roberts",
            "Gomez",
            "Phillips",
            "Evans",
        ]

        return choice(first_names) + " " + choice(last_names) + "'s Pad"

    @staticmethod
    def create_device_id(s: str = "") -> str:
        """生成设备ID。

        Args:
            s (str, optional): 用于生成ID的字符串. Defaults to "".

        Returns:
            str: 返回生成的设备ID
        """
        if s == "" or s == "string":
            s = "".join(choice(string.ascii_letters) for _ in range(15))
        md5_hash = hashlib.md5(s.encode()).hexdigest()
        return "49" + md5_hash[2:]

    async def start_auto_heartbeat(self) -> bool:
        """开始自动心跳。

        Returns:
            bool: 成功返回True，否则返回False

        Raises:
            UserLoggedOut: 如果未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise Exception("请先登录")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(
                f"{self.base_url}/AutoHeartbeatStart", json=json_param
            )
            json_resp = await response.json()

            if json_resp.get("Success"):
                return True
            else:
                raise Exception(json_resp)

    def error_handler(self, response):
        """处理API错误响应

        Args:
            response (dict): API响应

        Raises:
            Exception: 抛出对应的异常
        """
        message = response.get("Message", "未知错误")
        logger.error(f"API错误: {message}")
        raise Exception(f"API错误: {message}")
