from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.message.components import Plain, Image, Video, BaseMessageComponent
import asyncio
import re
import json
import os
import subprocess
import uuid
import aiohttp
import aiofiles
import requests  # 添加requests库用于同步HTTP请求
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urlparse, parse_qs
from asyncio import Semaphore


@register("astrbot_plugin_bilibili_video_downloader", "Xuewu", "B站视频下载器 - 命令触发模式下载B站视频", "1.6.2")  # 按AstrBot规范重构配置结构
class BilibiliVideoDownloaderPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        
        # 数据目录
        self.data_dir = Path(context._config.config_path).parent / "plugins" / "plugin_upload_astrbot_plugin_bilibili_video_downloader"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 必须使用AstrBot配置系统
        if config is None:
            raise ValueError("此插件需要AstrBot v4.0+的配置系统支持")
        
        self.config = config
        logger.info("使用AstrBot可视化配置系统")
        
        # 下载目录
        self.download_dir = self.data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        
        # 临时文件追踪
        self.temp_files: Set[str] = set()
        
        # 并发控制信号量
        self.semaphore = Semaphore(self.config.get('concurrent_downloads', 3))
        
        # 共享的HTTP会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 请求头 - 支持配置覆盖
        self.headers = {
            'User-Agent': self.config.get('advanced_settings', {}).get('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
            'Referer': self.config.get('advanced_settings', {}).get('referer', 'https://www.bilibili.com/'),
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        
        # 临时文件追踪
        self.temp_files: Set[str] = set()
        
        # 并发控制信号量
        self.semaphore = Semaphore(self.config.get('concurrent_downloads', 3))
        
        # 共享的HTTP会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 已移至构造函数中初始化
        
        # 检查ffmpeg
        self.ffmpeg_available = self._check_ffmpeg()
        if not self.ffmpeg_available:
            logger.warning("FFmpeg未安装，视频合并功能将不可用")
        
        logger.info(f"B站视频下载器已加载，下载目录: {self.download_dir}")

    async def initialize(self):
        """插件初始化 - 创建会话"""
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=timeout
        )
        logger.info("B站视频下载器初始化完成")

    async def terminate(self):
        """插件卸载时清理"""
        # 关闭HTTP会话
        if self.session and not self.session.closed:
            await self.session.close()
        
        # 清理临时文件
        await self._cleanup_temp_files()

    def _check_ffmpeg(self) -> bool:
        """检查ffmpeg是否可用"""
        try:
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
            return result.returncode == 0
        except:
            return False

    async def _cleanup_temp_files(self):
        """清理临时文件"""
        files_to_remove = list(self.temp_files)
        self.temp_files.clear()
        
        for file_path in files_to_remove:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"已删除临时文件: {file_path}")
            except Exception as e:
                logger.error(f"删除临时文件失败 {file_path}: {e}")


    def _extract_bvid_from_message(self, message: str) -> Optional[str]:
        """从消息中提取BV号"""
        logger.debug(f"开始解析消息: {message}")
        
        # 首先检查是否包含B站链接（包括短链接）
        # 检查完整链接格式
        full_url_patterns = [
            (r'https?://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})', "完整链接"),
            (r'https?://b23\.tv/([0-9A-Za-z]+)', "短链接"),
        ]
        
        for pattern, link_type in full_url_patterns:
            url_match = re.search(pattern, message)
            if url_match:
                if link_type == "短链接":
                    short_code = url_match.group(1)
                    logger.debug(f"找到短链接代码: {short_code}")
                    result = self._resolve_b23_short_link(short_code)
                    if result:
                        logger.debug(f"短链接解析成功: {result}")
                        return result
                    else:
                        logger.warning(f"短链接解析失败: {short_code}")
                        return None
                else:
                    bv_id = url_match.group(1)
                    logger.debug(f"找到完整链接BV号: {bv_id}")
                    return bv_id
        
        # 检查简写的b23.tv链接格式
        simple_b23_pattern = r'\b(b23\.tv/[0-9A-Za-z]+)\b'
        simple_match = re.search(simple_b23_pattern, message)
        if simple_match:
            full_short_url = simple_match.group(1)
            # 提取短代码部分
            short_code = full_short_url.split('/')[-1]
            logger.debug(f"找到简写短链接: {full_short_url}, 代码: {short_code}")
            result = self._resolve_b23_short_link(short_code)
            if result:
                logger.debug(f"简写短链接解析成功: {result}")
                return result
            else:
                logger.warning(f"简写短链接解析失败: {short_code}")
                return None
        
        # 匹配BV号格式（只有在没有链接的情况下才匹配）
        bv_pattern = r'\b(BV[0-9A-Za-z]{10})\b'
        bv_match = re.search(bv_pattern, message)
        if bv_match:
            bv_id = bv_match.group(1)
            logger.debug(f"找到BV号: {bv_id}")
            return bv_id
        
        # 匹配AV号并转换为BV号
        av_pattern = r'\bav(\d+)\b'
        av_match = re.search(av_pattern, message, re.IGNORECASE)
        if av_match:
            aid = int(av_match.group(1))
            bv_id = self._av2bv(aid)
            logger.debug(f"AV号 {aid} 转换为BV号: {bv_id}")
            return bv_id
        
        logger.debug("未找到任何有效的B站标识")
        return None

    def _resolve_b23_short_link(self, short_code: str) -> Optional[str]:
        """解析B站短链接获取真实BV号"""
        short_url = f"https://b23.tv/{short_code}"
        logger.info(f"开始解析短链接: {short_url}")
        
        try:
            logger.debug(f"发送HEAD请求到: {short_url}")
            # 使用同步requests发送HEAD请求获取重定向信息
            response = requests.head(
                short_url, 
                headers=self.headers,
                allow_redirects=True,  # 允许重定向
                timeout=10
            )
            
            logger.debug(f"请求完成，状态码: {response.status_code}")
            
            # 检查响应状态
            if response.status_code != 200:
                logger.warning(f"短链接请求失败，状态码: {response.status_code}")
                return None
            
            # 检查最终URL
            final_url = response.url
            logger.info(f"短链接 {short_url} 重定向到: {final_url}")
            
            # 从重定向URL中提取BV号
            # 首先尝试标准的/video/路径格式
            bv_pattern = r'/video/(BV[0-9A-Za-z]{10})'
            bv_match = re.search(bv_pattern, final_url)
            if bv_match:
                bv_id = bv_match.group(1)
                logger.info(f"✅ 成功从重定向URL中提取BV号: {bv_id}")
                return bv_id
            
            # 如果上面没匹配到，尝试其他可能的URL格式
            bv_pattern2 = r'BV[0-9A-Za-z]{10}'
            bv_match2 = re.search(bv_pattern2, final_url)
            if bv_match2:
                bv_id = bv_match2.group()
                logger.info(f"✅ 成功从重定向URL中提取BV号(备用模式): {bv_id}")
                return bv_id
                
            logger.warning(f"❌ 无法从重定向URL中提取BV号")
            logger.warning(f"重定向URL详情: {final_url}")
            return None
                
        except requests.exceptions.Timeout:
            logger.error(f"解析短链接超时 {short_url}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"解析短链接网络请求失败 {short_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"解析短链接失败 {short_url}: {e}")
            return None

    def _av2bv(self, aid: int) -> str:
        """AV号转BV号"""
        table = 'fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF'
        tr = {}
        for i in range(58):
            tr[table[i]] = i
        s = [11, 10, 3, 8, 4, 6]
        xor = 177451812
        add = 8728348608
        
        aid = (aid ^ xor) + add
        r = list('BV1  4 1 7  ')
        for i in range(6):
            r[s[i]] = table[aid // 58**i % 58]
        return ''.join(r)

    async def _get_video_info(self, bvid: str) -> Optional[Dict[str, Any]]:
        """获取视频基本信息"""
        if not self.session:
            return None
            
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('code') == 0:
                        return data.get('data', {})
        except Exception as e:
            logger.error(f"获取视频信息失败 {bvid}: {e}")
        
        return None

    async def _get_video_urls(self, bvid: str) -> tuple[Optional[str], Optional[str]]:
        """获取视频和音频URL"""
        if not self.session:
            return None, None
        
        url = f"https://www.bilibili.com/video/{bvid}"
        
        try:
            async with self.semaphore:
                async with self.session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        logger.error(f"获取页面失败: {response.status}")
                        return None, None
                    
                    html_content = await response.text()
            
            # 提取 window.__playinfo__
            playinfo_match = re.search(r'window\.__playinfo__=(.*?)</script>', html_content)
            if not playinfo_match:
                logger.error(f"未找到playinfo: {bvid}")
                return None, None
            
            playinfo = json.loads(playinfo_match.group(1))
            
            # 解析视频和音频URL
            if 'data' in playinfo and 'dash' in playinfo['data']:
                video_qualities = playinfo['data']['dash'].get('video', [])
                audio_qualities = playinfo['data']['dash'].get('audio', [])
                
                if not video_qualities:
                    logger.error(f"没有找到视频流: {bvid}")
                    return None, None
                
                if not audio_qualities:
                    logger.error(f"没有找到音频流: {bvid}")
                    return None, None
                
                # 选最高画质
                best_video = max(video_qualities, key=lambda x: x.get('bandwidth', 0))
                best_audio = max(audio_qualities, key=lambda x: x.get('bandwidth', 0))
                
                video_url = best_video.get('baseUrl')
                audio_url = best_audio.get('baseUrl')
                
                return video_url, audio_url
            
            return None, None
            
        except json.JSONDecodeError as e:
            logger.error(f"解析playinfo JSON失败 {bvid}: {e}")
            return None, None
        except Exception as e:
            logger.error(f"获取视频URL失败 {bvid}: {e}")
            return None, None

    async def _download_file(self, url: str, output_path: str) -> bool:
        """下载文件"""
        if not self.session:
            return False
        
        try:
            async with self.semaphore:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"下载失败: HTTP {response.status}")
                        return False
                    
                    async with aiofiles.open(output_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)
                    
                    return True
                    
        except Exception as e:
            logger.error(f"下载出错: {e}")
            return False

    async def _merge_video_audio(self, video_path: str, audio_path: str, output_path: str) -> bool:
        """合并视频和音频"""
        try:
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-y',
                output_path,
                '-hide_banner',
                '-loglevel', 'error'
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                # 删除临时文件
                for path in [video_path, audio_path]:
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                            self.temp_files.discard(path)
                    except Exception as e:
                        logger.warning(f"删除临时文件失败 {path}: {e}")
                return True
            else:
                logger.error(f"合并失败: {stderr.decode()[:200]}")
                return False
                
        except Exception as e:
            logger.error(f"合并出错: {e}")
            return False

    def _clean_filename(self, filename: str) -> str:
        """清理文件名 - 移除可能导致兼容性问题的特殊字符"""
        if not filename:
            return "untitled"
        
        # 移除或替换可能导致兼容性问题的特殊字符
        # 包括中文标点符号、百分号、斜杠等
        cleaned = re.sub(r'[<>:"/\\|?*\u3000-\u303F\uFF00-\uFFEF%]', '_', filename)
        
        # 移除前导和尾随的下划线和空格
        cleaned = cleaned.strip('_ ').strip()
        
        # 确保不为空
        if not cleaned:
            return "untitled"
        
        return cleaned

    async def _download_video(self, bvid: str, video_info: Dict[str, Any]) -> Optional[str]:
        """下载单个视频"""
        title = video_info.get('title', '未知标题')
        
        try:
            # 清理文件名
            cleaned_title = self._clean_filename(title)
            unique_id = str(uuid.uuid4())[:8]
            
            # 临时文件路径
            video_temp = self.download_dir / f"video_{unique_id}.m4s"
            audio_temp = self.download_dir / f"audio_{unique_id}.m4s"
            output_path = self.download_dir / f"{cleaned_title}_{unique_id}.mp4"
            
            # 记录临时文件
            self.temp_files.add(str(video_temp))
            self.temp_files.add(str(audio_temp))
            self.temp_files.add(str(output_path))
            
            # 获取视频和音频URL
            logger.info(f"获取下载地址: {bvid}")
            video_url, audio_url = await self._get_video_urls(bvid)
            
            if not video_url:
                logger.error(f"获取视频地址失败: {bvid}")
                return None
            
            if not audio_url:
                logger.error(f"获取音频地址失败: {bvid}")
                return None
            
            # 下载视频
            logger.info(f"下载视频: {title}")
            video_success = await self._download_file(video_url, str(video_temp))
            if not video_success:
                logger.error(f"视频下载失败: {bvid}")
                return None
            
            # 下载音频
            logger.info(f"下载音频: {title}")
            audio_success = await self._download_file(audio_url, str(audio_temp))
            if not audio_success:
                logger.error(f"音频下载失败: {bvid}")
                return None
            
            # 合并
            if self.ffmpeg_available:
                logger.info(f"合并视频音频: {title}")
                merge_success = await self._merge_video_audio(str(video_temp), str(audio_temp), str(output_path))
                if merge_success:
                    # 记录文件信息用于调试
                    file_size = os.path.getsize(str(output_path))
                    logger.info(f"下载完成: {output_path} (大小: {file_size} bytes)")
                    return str(output_path)
                else:
                    logger.error("合并失败")
                    return None
            else:
                logger.error("FFmpeg不可用，无法合并")
                return None
                
        except Exception as e:
            logger.error(f"下载视频异常: {e}")
            return None

    def _create_video_message(self, video_info: Dict[str, Any], file_path: str) -> List[BaseMessageComponent]:
        """创建视频消息链"""
        title = video_info.get('title', '未知标题')
        author = video_info.get('owner', {}).get('name', '未知UP主')
        play = video_info.get('stat', {}).get('view', 0)
        duration = video_info.get('duration', 0)
        bvid = video_info.get('bvid', '')
        
        # 转换时长格式
        minutes = duration // 60
        seconds = duration % 60
        duration_str = f"{minutes:02d}:{seconds:02d}"
        
        # 文字消息
        text_msg = (
            f"【B站视频下载】\n"
            f"标题：{title}\n"
            f"UP主：{author}\n"
            f"时长：{duration_str}\n"
            f"播放量：{play}\n"
            f"链接：https://www.bilibili.com/video/{bvid}"
        )
        
        chain: List[BaseMessageComponent] = [Plain(text_msg)]
        
        # 添加视频文件 - 使用跨平台兼容的方式
        if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            # 直接使用本地文件路径，避免file:///协议导致的兼容性问题
            video = Video(file=file_path, path=file_path)
            chain.append(video)  # 正确添加视频组件到列表
        
        return chain

    async def _cleanup_after_send(self, file_path: str):
        """发送后清理文件"""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                self.temp_files.discard(file_path)
                logger.info(f"已删除文件: {file_path}")
        except Exception as e:
            logger.error(f"删除文件失败: {e}")


    @filter.command("b23 toggle_limit")
    async def toggle_limit_command(self, event: AstrMessageEvent):
        """切换时长限制命令：开启/关闭视频时长限制"""
        # 切换时长限制状态
        current_state = self.config.get('enable_duration_limit', True)
        new_state = not current_state
        
        # 更新配置
        self.config['enable_duration_limit'] = new_state
        
        # 状态文字
        state_text = "开启" if new_state else "关闭"
        opposite_text = "关闭" if new_state else "开启"
        
        result_text = [
            f"✅ 视频时长限制已{state_text}",
            f"当前状态：{'限制启用' if new_state else '限制关闭'}",
            f"要{opposite_text}限制，请再次使用此命令"
        ]
        
        yield event.plain_result("\n".join(result_text))

    @filter.command("b23 help")
    async def help_command(self, event: AstrMessageEvent):
        """帮助命令"""
        help_text = [
            "=== B站视频下载器帮助 ===",
            "📥 支持的视频标识格式：",
            "• BV号: BV1xxxxxxxxxx",
            "• AV号: av123456",
            "• 完整链接: https://www.bilibili.com/video/BV1xxxxxxxxxx",
            "• 短链接: https://b23.tv/xxxxxxx 或 b23.tv/xxxxxxx",
            "",
            "📥 下载命令：",
            "/b23 d 支持的视频标识格式",
            "",
            "🖼️ 获取视频封面：",
            "/b23 cover 支持的视频标识格式",
            "",
            "🔧 控制命令：",
            "/b23 toggle_limit - 开启/关闭视频时长限制",
            "/b23 test https://b23.tv/xxxxxx  (测试短链接解析)",
            "",
            "📋 其他命令：",
            "/b23 help - 显示帮助信息",
            "/b23 config - 查看当前配置",
            "/b23 stats - 查看统计信息",
            "/b23 clean - 清理临时文件",
            "",
            "⚙️ 配置管理：",
            "请在 AstrBot 管理面板的插件配置页面进行可视化配置",
            "支持设置下载限制、质量、并发数等参数",
            "",
            "⚠️ 注意事项：",
            "• 时长限制可在配置中开启/关闭，关闭后可下载任意时长视频",
            "• 需要安装FFmpeg才能使用下载功能",
            "• 下载前会显示视频详细信息",
            "• 已优化文件名处理，自动清理特殊字符确保跨平台兼容性",
            "• 支持包含百分号(%)等特殊字符的视频标题"
        ]
        yield event.plain_result("\n".join(help_text))

    @filter.command("b23 config")
    async def config_command(self, event: AstrMessageEvent):
        """配置命令：查看插件配置"""
        # 由于使用了可视化配置系统，这里只需显示当前配置信息
        duration_settings = self.config.get('duration_settings', {})
        enable_limit = duration_settings.get('enable_limit', True)
        max_duration = duration_settings.get('max_duration', 600)
        
        limit_status = '开启' if enable_limit else '关闭'
        minutes = max_duration // 60
        
        config_info = [
            "=== 当前配置 ===",
            f"时长限制: {limit_status}",
            f"最大时长: {max_duration}秒 ({minutes}分钟)" if enable_limit else "最大时长: 未启用",
            f"下载质量: {self.config.get('download_quality', 'highest')}",
            f"自动清理: {'开启' if self.config.get('auto_cleanup', True) else '关闭'}",
            f"并发下载: {self.config.get('concurrent_downloads', 3)}",
            f"超时时间: {self.config.get('timeout', 30)}秒",
            f"私聊启用: {'开启' if self.config.get('enable_private_chat', True) else '关闭'}",
            f"群聊启用: {'开启' if self.config.get('enable_group_chat', True) else '关闭'}",
        ]
        yield event.plain_result("\n".join(config_info))

    @filter.command("b23 stats")
    async def stats_command(self, event: AstrMessageEvent):
        """统计命令：查看下载统计信息"""
        download_count = len(list(self.download_dir.glob("*.mp4")))
        temp_count = len(self.temp_files)
        
        stats_info = [
            "=== 下载统计 ===",
            f"已下载视频: {download_count} 个",
            f"临时文件: {temp_count} 个",
            f"插件版本: 1.6.2",
        ]
        yield event.plain_result("\n".join(stats_info))

    @filter.command("b23 clean")
    async def clean_command(self, event: AstrMessageEvent):
        """清理命令：手动清理临时文件"""
        count = len(self.temp_files)
        await self._cleanup_temp_files()
        yield event.plain_result(f"✅ 已清理 {count} 个临时文件")

    @filter.command("b23 cover")
    async def cover_command(self, event: AstrMessageEvent):
        """封面命令：获取B站视频封面"""
        message = event.message_str.strip()
        
        # 检查是否启用对应聊天类型
        is_group = event.get_message_type().value == "GroupMessage"
        if is_group and not self.config['enable_group_chat']:
            yield event.plain_result("❌ 群聊功能已禁用")
            return
        if not is_group and not self.config['enable_private_chat']:
            yield event.plain_result("❌ 私聊功能已禁用")
            return
        
        # 提取BV号
        bvid = self._extract_bvid_from_message(message)
        
        if not bvid:
            yield event.plain_result("❌ 请提供有效的B站视频链接、BV号或AV号\n示例：/b23 cover BV1xxxxxxxxxx")
            return
        
        # 获取视频信息
        video_info = await self._get_video_info(bvid)
        if not video_info:
            yield event.plain_result("❌ 无法获取视频信息，请检查BV号是否正确")
            return
        
        try:
            # 获取封面图片URL
            pic_url = video_info.get('pic', '')
            if not pic_url:
                yield event.plain_result("❌ 无法获取视频封面信息")
                return
            
            # 如果URL不是完整URL，补充协议和域名
            if pic_url.startswith('//'):
                pic_url = 'https:' + pic_url
            elif pic_url.startswith('/'):
                pic_url = 'https://i0.hdslb.com' + pic_url
            
            title = video_info.get('title', '未知标题')
            author = video_info.get('owner', {}).get('name', '未知UP主')
            
            # 创建消息链
            chain = [
                Plain(f"📺 视频封面\n标题：{title}\nUP主：{author}"),
                Image.fromURL(pic_url)
            ]
            
            yield event.chain_result(chain)  # 现在传入的是正确的组件列表
            
        except Exception as e:
            logger.error(f"获取视频封面时出错: {e}")
            yield event.plain_result("❌ 获取封面过程中出现错误")

    @filter.command("b23 d")
    # @filter.command("b23 d")  # 添加简写命令
    async def download_command(self, event: AstrMessageEvent):
        """下载命令：手动触发B站视频下载"""
        message = event.message_str.strip()
        
        # 检查是否启用对应聊天类型
        is_group = event.get_message_type().value == "GroupMessage"
        if is_group and not self.config['enable_group_chat']:
            yield event.plain_result("❌ 群聊下载功能已禁用")
            return
        if not is_group and not self.config['enable_private_chat']:
            yield event.plain_result("❌ 私聊下载功能已禁用")
            return
        
        # 提取BV号
        bvid = self._extract_bvid_from_message(message)
        
        if not bvid:
            yield event.plain_result("❌ 请提供有效的B站视频链接、BV号或AV号\n示例：/b23 d BV1xxxxxxxxxx")
            return
        
        # 获取视频信息
        video_info = await self._get_video_info(bvid)
        if not video_info:
            yield event.plain_result("❌ 无法获取视频信息，请检查BV号是否正确")
            return
        
        # 检查视频时长
        duration = video_info.get('duration', 0)
        # 从嵌套配置中获取时长限制设置
        duration_settings = self.config.get('duration_settings', {})
        enable_duration_limit = duration_settings.get('enable_limit', True)
        max_duration = duration_settings.get('max_duration', 600)
        
        if enable_duration_limit and duration > max_duration:
            minutes = max_duration // 60
            yield event.plain_result(f"❌ 视频时长超过限制({minutes}分钟)，暂不支持下载")
            return
        
        # 显示详细的视频信息
        title = video_info.get('title', '未知标题')
        author = video_info.get('owner', {}).get('name', '未知UP主')
        play_count = video_info.get('stat', {}).get('view', 0)
        
        # 格式化播放量
        if play_count >= 10000:
            play_display = f"{play_count/10000:.1f}万"
        else:
            play_display = str(play_count)
        
        # 转换时长格式
        minutes = duration // 60
        seconds = duration % 60
        duration_str = f"{minutes:02d}:{seconds:02d}"
        
        # 显示视频详情
        detail_info = (
            f"📥 准备下载视频\n"
            f"标题：{title}\n"
            f"UP主：{author}\n"
            f"时长：{duration_str}\n"
            f"播放量：{play_display}\n"
            f"正在开始下载..."
        )
        yield event.plain_result(detail_info)
        
        try:
            # 下载视频
            file_path = await self._download_video(bvid, video_info)
            
            if not file_path or not os.path.exists(file_path):
                yield event.plain_result("❌ 视频下载失败")
                return
            
            # 发送视频
            chain = self._create_video_message(video_info, file_path)
            yield event.chain_result(chain)  # 现在传入的是正确的组件列表
            
            # 清理文件
            if self.config['auto_cleanup']:
                await self._cleanup_after_send(file_path)
            
        except Exception as e:
            logger.error(f"处理视频下载时出错: {e}")
            yield event.plain_result("❌ 处理过程中出现错误")

    @filter.command("b23 test")
    async def test_command(self, event: AstrMessageEvent):
        """测试命令：调试短链接解析功能"""
        message = event.message_str.strip()
        logger.info(f"收到测试命令: {message}")
        
        # 显示原始输入分析
        test_info = [
            "=== 短链接解析测试 ===",
            f"原始消息: {message}",
            ""
        ]
        
        # 提取BV号
        bvid = self._extract_bvid_from_message(message)
        
        if bvid:
            test_info.extend([
                "✅ 链接识别成功",
                f"提取的标识: {bvid}",
                ""
            ])
            
            # 尝试获取视频信息
            test_info.append("正在获取视频信息...")
            video_info = await self._get_video_info(bvid)
            if video_info:
                title = video_info.get('title', '未知标题')
                author = video_info.get('owner', {}).get('name', '未知UP主')
                duration = video_info.get('duration', 0)
                play_count = video_info.get('stat', {}).get('view', 0)
                
                # 格式化时长
                minutes = duration // 60
                seconds = duration % 60
                duration_str = f"{minutes:02d}:{seconds:02d}"
                
                # 格式化播放量
                if play_count >= 10000:
                    play_display = f"{play_count/10000:.1f}万"
                else:
                    play_display = str(play_count)
                
                test_info.extend([
                    "✅ 视频信息获取成功",
                    f"标题: {title}",
                    f"UP主: {author}",
                    f"时长: {duration_str}",
                    f"播放量: {play_display}",
                    f"BV号: {bvid}"
                ])
            else:
                test_info.extend([
                    "❌ 无法获取视频信息",
                    "可能的原因:",
                    "• BV号格式不正确",
                    "• 视频已被删除",
                    "• 网络连接问题",
                    "• B站API限制"
                ])
        else:
            test_info.extend([
                "❌ 链接识别失败",
                "未能从中提取有效的B站标识",
                "",
                "支持的格式:",
                "• BV号: BV1xxxxxxxxxx",
                "• AV号: av123456", 
                "• 完整链接: https://www.bilibili.com/video/BV1xxxxxxxxxx",
                "• 短链接: https://b23.tv/xxxxxxx 或 b23.tv/xxxxxxx"
            ])
        
        yield event.plain_result("\n".join(test_info))
