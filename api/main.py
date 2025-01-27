import time
import socket
from flask import Flask, jsonify, request
from mcstatus import JavaServer
from flask_cors import CORS
from mcrcon import MCRcon, MCRconException
import os
import re
import logging
from dotenv import load_dotenv
from functools import lru_cache
from typing import Dict, List, Tuple, Optional
import json
import struct
from pathlib import Path

# 初始化环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 记录服务器启动时间
SERVER_START_TIME = time.time()

# 创建Flask应用
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # 禁用ASCII编码转义
CORS(app)


# 服务器配置校验
def validate_config() -> Tuple[str, int, str, str]:
    """验证并返回服务器配置"""
    try:
        rcon_host = os.environ["RCON_HOST"]
        rcon_port = int(os.environ.get("RCON_PORT", "25575"))
        rcon_password = os.environ["RCON_PASSWORD"]
        server_address = os.environ["SERVER_ADDRESS"]
        return rcon_host, rcon_port, rcon_password, server_address
    except (KeyError, ValueError) as e:
        logger.critical(f"配置错误: {str(e)}")
        raise


RCON_HOST, RCON_PORT, RCON_PASSWORD, SERVER_ADDRESS = validate_config()

# 维度映射（带缓存）
DIMENSION_MAP_PATH = Path("dimension_map.json")


def load_dimension_map() -> dict:
    """从配置文件加载维度映射"""
    if DIMENSION_MAP_PATH.exists():
        try:
            with open(DIMENSION_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"配置文件加载失败: {str(e)}")
            return {}
    return {}


@lru_cache(maxsize=32)
def get_dimension_display_name(raw_dim: str) -> str:
    """获取维度的友好显示名称"""
    dimension_map = load_dimension_map()
    return dimension_map.get(raw_dim, raw_dim.split(":")[-1])


# RCON客户端封装
class RCONClient:
    """带无限重连机制的RCON客户端"""

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.conn: Optional[MCRcon] = None
        self.last_connect_time = 0
        self.connect_cooldown = 10  # 基础重连间隔（秒）
        self.max_wait = 300  # 最大重试间隔（5分钟）

    def _calculate_wait(self, attempt: int) -> int:
        """计算指数退避等待时间"""
        wait = min(self.connect_cooldown * (2 ** attempt), self.max_wait)
        return wait

    def connect(self) -> None:
        """持续尝试连接直到成功"""
        attempt = 0
        while True:
            try:
                self.disconnect()  # 清理旧连接
                self.conn = MCRcon(self.host, self.password, self.port)
                self.conn.connect()
                logger.info("RCON连接成功")
                self.last_connect_time = time.time()
                return
            except (MCRconException, socket.error, TimeoutError) as e:
                wait = self._calculate_wait(attempt)
                logger.warning(f"连接失败，{wait}秒后重试... 错误: {str(e)}")
                time.sleep(wait)
                attempt += 1
            except TypeError as te:  # 处理mcrcon库的类型错误
                logger.warning(f"检测到mcrcon类型错误，尝试修复连接...")
                self.conn = MCRcon(
                    host=self.host,
                    password=self.password,
                    port=self.port  # 确保显式指定端口参数
                )

    def execute(self, command: str) -> str:
        """执行命令（带无限重试）"""
        while True:
            try:
                if not self.conn:
                    self.connect()
                logger.debug(f"正在执行命令: {command}")
                response = self.conn.command(command)
                logger.debug(f"收到响应: {response[:200]}")  # 截断长响应
                return response
            except (MCRconException, ConnectionResetError, socket.error, struct.error) as e:
                logger.error(f"命令执行失败（{e.__class__.__name__}）: {command} - {str(e)}")
                self.disconnect()
                time.sleep(self.connect_cooldown)
            except Exception as e:
                logger.error(f"未知错误: {str(e)}")
                self.disconnect()
                time.sleep(self.connect_cooldown)

    def disconnect(self):
        """安全断开连接"""
        if self.conn:
            try:
                self.conn.disconnect()
            except Exception as e:
                logger.debug(f"断开连接异常: {str(e)}")
            finally:
                self.conn = None
        logger.debug("RCON连接已清理")

    def __del__(self):
        self.disconnect()


# 全局RCON客户端实例
rcon_client = RCONClient(RCON_HOST, RCON_PORT, RCON_PASSWORD)


# 玩家数据解析
class PlayerDataParser:
    POS_PATTERN = re.compile(r"Pos:\s*\[([\d., dE+-]+)]")
    DIMENSION_PATTERN = re.compile(r'Dimension:\s*"([^"]+)"')
    PLAYERNAME_PATTERN = re.compile(r"([a-zA-Z0-9_]{3,16})(?:,|$)")
    HEALTH_PATTERN = re.compile(r"Health:\s*([0-9.]+)[fs]")
    FOOD_PATTERN = re.compile(r"(?:foodLevel|food):\s*(\d+)(?:s|b|)")
    LEVEL_PATTERN = re.compile(r"(?:XpLevel|level):\s*(\d+)(?:s|b|)")

    @classmethod
    def parse_players(cls, list_response: str) -> List[str]:
        matches = cls.PLAYERNAME_PATTERN.findall(list_response)
        return list(set(matches)) if matches else []

    @classmethod
    def parse_entity_data(cls, response: str) -> Dict:
        result = {
            "pos": None,
            "dimension": None,
            "health": None,
            "food": None,
            "level": None
        }

        try:
            # 坐标解析
            if pos_match := cls.POS_PATTERN.search(response):
                pos_str = pos_match.group(1).replace('d', '').strip()
                try:
                    x, y, z = map(float, pos_str.split(", "))
                    result["pos"] = (x, y, z)
                except ValueError:
                    logger.warning(f"坐标格式错误: {pos_str}")

            # 维度解析
            if dim_match := cls.DIMENSION_PATTERN.search(response):
                raw_dim = dim_match.group(1)
                result["dimension"] = {
                    "raw": raw_dim,
                    "display": get_dimension_display_name(raw_dim)
                }

            # 生命值解析
            if health_match := cls.HEALTH_PATTERN.search(response):
                health_val = health_match.group(1)
                try:
                    result["health"] = float(health_val) if '.' in health_val else int(health_val)
                except (ValueError, TypeError):
                    logger.warning(f"生命值转换失败: {health_val}")

            # 饱食度解析
            if food_match := cls.FOOD_PATTERN.search(response):
                try:
                    result["food"] = int(food_match.group(1))
                except (ValueError, TypeError):
                    logger.warning(f"饱食度转换失败: {food_match.group(1)}")

            # 等级解析
            if level_match := cls.LEVEL_PATTERN.search(response):
                try:
                    result["level"] = int(level_match.group(1))
                except (ValueError, TypeError):
                    logger.warning(f"等级转换失败: {level_match.group(1)}")

        except Exception as e:
            logger.error(f"解析异常: {str(e)}")

        return {k: v for k, v in result.items() if v is not None}


# API端点实现
@app.route('/api/player/avatar', methods=['GET'])
def get_avatar():
    if not (uuid := request.args.get('uuid')):
        return jsonify({"error": "缺少UUID参数"}), 400
    return jsonify({
        "uuid": uuid,
        "avatar_url": f"https://crafatar.com/avatars/{uuid}?overlay"
    })


@app.route('/api/server/status', methods=['GET'])
def get_server_status():
    """获取服务器状态"""
    try:
        server = JavaServer(SERVER_ADDRESS, timeout=5)
        status = server.status()
        players = [{
            "name": p.name,
            "uuid": p.id,
            "avatar_url": f"https://crafatar.com/avatars/{p.id}"
        } for p in status.players.sample] if status.players.sample else []

        # 获取主世界时间和游戏运行时间
        world_time = None
        game_time = None
        try:
            time_response = rcon_client.execute("time query daytime")
            if "The time is" in time_response:
                world_time = int(time_response.split(" ")[-1])
            game_time_response = rcon_client.execute("time query gametime")
            if "The time is" in game_time_response:
                game_time = int(game_time_response.split(" ")[-1])
        except Exception as e:
            logger.warning(f"获取时间失败: {str(e)}")

        # 计算服务器运行时间
        uptime_seconds = int(time.time() - SERVER_START_TIME)
        uptime_formatted = format_uptime(uptime_seconds)

        return jsonify({
            "online": status.players.online,
            "latency": status.latency,
            "version": status.version.name,
            "players": players,
            "world_time": world_time,
            "world_time_formatted": format_minecraft_time(world_time) if world_time is not None else None,
            "game_time": game_time,
            "game_time_formatted": format_minecraft_time(game_time) if game_time is not None else None,
            "uptime_seconds": uptime_seconds,
            "uptime_formatted": uptime_formatted
        })
    except Exception as e:
        logger.error(f"服务器状态查询失败: {str(e)}")
        return jsonify({"error": "无法获取服务器状态"}), 500


def format_uptime(seconds: int) -> str:
    """将秒数格式化为 HH:MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_minecraft_time(ticks: Optional[int]) -> str:
    if not ticks:
        return "未知"
    ticks %= 24000
    hours = (ticks // 1000 + 6) % 24
    minutes = int((ticks % 1000) / 1000 * 60)
    return f"{hours:02}:{minutes:02}"


@app.route('/api/server/player_info', methods=['GET'])
def get_player_infos():
    try:
        list_resp = rcon_client.execute("list")
        logger.debug(f"玩家列表响应: {list_resp}")
        if "There are 0" in list_resp:
            return jsonify([])

        players = PlayerDataParser.parse_players(list_resp)
        if not players:
            return jsonify([])

        results = []
        for player in players:
            try:
                entity_resp = rcon_client.execute(f"data get entity {player}")

                # 新增响应有效性检查
                if "No entity was found" in entity_resp:
                    logger.warning(f"玩家 {player} 不存在")
                    continue
                if "Unable to find entity" in entity_resp:
                    logger.warning(f"无法定位玩家 {player}")
                    continue

                logger.debug(f"{player} 的实体数据: {entity_resp[:200]}")  # 截断长响应
                data = PlayerDataParser.parse_entity_data(entity_resp)

                if not data.get("pos") or not data.get("dimension"):
                    logger.warning(f"玩家 {player} 数据不完整，跳过")
                    continue

                results.append({
                    "name": player,
                    "dimension": data["dimension"],
                    "position": {
                        "x": round(data["pos"][0], 1),
                        "y": round(data["pos"][1], 1),
                        "z": round(data["pos"][2], 1)
                    },
                    "status": {k: v for k, v in data.items() if k in ["health", "food", "level"]}
                })
            except Exception as pe:
                logger.error(f"处理玩家 {player} 时出错: {str(pe)}", exc_info=True)

        return jsonify(results)
    except Exception as e:
        logger.error(f"获取玩家信息失败: {str(e)}", exc_info=True)
        return jsonify({"error": "内部服务器错误"}), 500


# 健康检查端点
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "uptime": int(time.time() - SERVER_START_TIME),
        "rcon_connected": rcon_client.conn is not None
    })


if __name__ == '__main__':
    try:
        # 初始化连接
        rcon_client.connect()
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        logger.info("正在关闭服务器...")
        rcon_client.disconnect()
    finally:
        rcon_client.disconnect()
