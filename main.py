import asyncio
import json
import re
import tomllib
import traceback
from typing import List, Optional, Union

import aiohttp
import filetype
from loguru import logger
import random

from WechatAPI import WechatAPIClient
from database.XYBotDB import XYBotDB
from utils.decorators import *
from utils.plugin_base import PluginBase
import os
import base64
import asyncio
import shutil
import subprocess  # 导入 subprocess 模块


class VideoSender(PluginBase):
    """
    一个点击链接获取视频并发送给用户的插件，支持多个视频源。
    """

    description = "随机播放小姐姐视频"
    author = "老夏的金库"
    version = "1.1.0"

    def __init__(self):
        super().__init__()
        # 确保 self.ffmpeg_path 始终有值
        self.ffmpeg_path = "/usr/bin/ffmpeg"  # 设置默认值
        try:
            with open("plugins/VideoSender/config.toml", "rb") as f:
                plugin_config = tomllib.load(f)
            config = plugin_config["VideoSender"]
            self.enable = config["enable"]
            self.commands = config["commands"]
            self.ffmpeg_path = config.get("ffmpeg_path", "/usr/bin/ffmpeg")  # ffmpeg 路径
            self.video_sources = config.get("video_sources", [])  # 视频源列表

            logger.info("VideoSender 插件配置加载成功")
        except FileNotFoundError:
            logger.error("VideoSender 插件配置文件未找到，插件已禁用。")
            self.enable = False
            self.commands = ["发送视频", "来个视频"]
            self.video_sources = []
        except Exception as e:
            logger.exception(f"VideoSender 插件初始化失败: {e}")
            self.enable = False
            self.commands = ["发送视频", "来个视频"]
            self.video_sources = []

        self.ffmpeg_available = self._check_ffmpeg()  # 在配置加载完成后检查 ffmpeg

    def _check_ffmpeg(self) -> bool:
        """检查 ffmpeg 是否可用"""
        try:
            process = subprocess.run([self.ffmpeg_path, "-version"], check=False, capture_output=True)
            if process.returncode == 0:
                logger.info(f"ffmpeg 可用，版本信息：{process.stdout.decode()}")
                return True
            else:
                logger.warning(f"ffmpeg 执行失败，返回码: {process.returncode}，错误信息: {process.stderr.decode()}")
                return False
        except FileNotFoundError:
            logger.warning(f"ffmpeg 未找到，路径: {self.ffmpeg_path}")
            return False
        except Exception as e:
            logger.exception(f"检查 ffmpeg 失败: {e}")
            return False

    async def _get_video_url(self, source_name: str = "") -> str:
        """
        根据视频源名称获取视频URL。

        Args:
            source_name (str, optional): 视频源名称. Defaults to "".

        Returns:
            str: 视频URL.
        """
        if not self.video_sources:
            logger.error("没有配置视频源")
            return ""

        if source_name:
            # 查找指定名称的视频源
            for source in self.video_sources:
                if source["name"] == source_name:
                    url = source["url"]
                    logger.debug(f"使用视频源: {source['name']}")
                    break
            else:
                logger.warning(f"未找到名为 {source_name} 的视频源，随机选择一个视频源")
                url = random.choice(self.video_sources)["url"]
                logger.debug(f"随机使用视频源: {url}")
        else:
            # 随机选择一个视频源
            source = random.choice(self.video_sources)
            url = source["url"]
            logger.debug(f"随机使用视频源: {source['name']}")

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:  # 添加超时
                async with session.get(url) as response:
                    if response.status == 200:
                        return str(response.url)  # 返回最终的 URL
                    else:
                        logger.error(f"获取视频失败，状态码: {response.status}")
                        return ""
        except Exception as e:
            logger.exception(f"获取视频过程中发生异常: {e}")
            return ""

    async def _download_video(self, video_url: str) -> bytes:
        """下载视频文件"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:  # 添加超时
                async with session.get(video_url) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        logger.error(f"下载视频失败，状态码: {response.status}")
                        return b""  # 返回空字节
        except Exception as e:
            logger.exception(f"下载视频过程中发生异常: {e}")
            return b""  # 返回空字节

    async def _extract_thumbnail_from_video(self, video_data: bytes) -> Optional[str]:
        """从视频数据中提取缩略图"""
        temp_dir = "temp_videos"  # 创建临时文件夹
        os.makedirs(temp_dir, exist_ok=True)
        video_path = os.path.join(temp_dir, "temp_video.mp4")
        thumbnail_path = os.path.join(temp_dir, "temp_thumbnail.jpg")

        try:
            with open(video_path, "wb") as f:
                f.write(video_data)

            # 异步执行 ffmpeg 命令
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg_path,
                "-i", video_path,
                "-ss", "00:00:01",  # 从视频的第 1 秒开始提取
                "-vframes", "1",
                thumbnail_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"ffmpeg 执行失败: {stderr.decode()}")
                return None

            with open(thumbnail_path, "rb") as image_file:
                image_data = image_file.read()
                image_base64 = base64.b64encode(image_data).decode("utf-8")
                return image_base64

        except FileNotFoundError:
            logger.error("ffmpeg 未找到，无法提取缩略图")
            return None
        except Exception as e:
            logger.exception(f"提取缩略图失败: {e}")
            return None
        finally:
            # 清理临时文件
            shutil.rmtree(temp_dir, ignore_errors=True)  # 递归删除临时文件夹

    @on_text_message
    async def handle_text_message(self, bot: WechatAPIClient, message: dict):
        """处理文本消息，判断是否需要触发发送视频。"""
        if not self.enable:
            return

        content = message["Content"].strip()
        chat_id = message["FromWxid"]

        for command in self.commands:
            if content == command:
                if command == "随机视频":
                    source_name = ""  # 随机选择
                elif command == "视频目录":
                    source_list = "\n".join([source["name"] for source in self.video_sources])
                    await bot.send_text_message(chat_id, f"可用的视频系列：\n{source_list}")
                    return
                else:
                    source_name = command  # 命令就是视频源名称

                try:
                    video_url = await self._get_video_url(source_name)

                    if video_url:
                        logger.info(f"获取到视频链接: {video_url}")
                        video_data = await self._download_video(video_url)

                        if video_data:
                            image_base64 = None
                            if self.ffmpeg_available:
                                # 获取缩略图
                                image_base64 = await self._extract_thumbnail_from_video(video_data)

                                if image_base64:
                                    logger.info("成功提取缩略图")
                                else:
                                    logger.warning("未能成功提取缩略图")
                            else:
                                await bot.send_text_message(chat_id, "由于 ffmpeg 未安装，无法提取缩略图。")

                            try:
                                video_base64 = base64.b64encode(video_data).decode("utf-8")

                                # 发送视频消息
                                await bot.send_video_message(chat_id, video=video_base64, image=image_base64 or "None")
                                logger.info(f"成功发送视频到 {chat_id}")

                            except binascii.Error as e:
                                logger.error(f"Base64 编码失败： {e}")
                                await bot.send_text_message(chat_id, "视频编码失败，请稍后重试。")

                            except Exception as e:
                                logger.exception(f"发送视频过程中发生异常: {e}")
                                await bot.send_text_message(chat_id, f"发送视频过程中发生异常，请稍后重试: {e}")

                        else:
                            logger.warning(f"未能下载到有效的视频数据")
                            await bot.send_text_message(chat_id, "未能下载到有效的视频，请稍后重试。")

                    else:
                        logger.warning(f"未能获取到有效的视频链接")
                        await bot.send_text_message(chat_id, "未能获取到有效的视频，请稍后重试。")

                except Exception as e:
                    logger.exception(f"处理视频过程中发生异常: {e}")
                    await bot.send_text_message(chat_id, f"处理视频过程中发生异常，请稍后重试: {e}")
                return # 找到匹配的命令后，结束循环

    async def close(self):
        """插件关闭时执行的操作。"""
        logger.info("VideoSender 插件已关闭")