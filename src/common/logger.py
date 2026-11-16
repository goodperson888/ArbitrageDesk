import sys
from loguru import logger
from pathlib import Path
from typing import Optional


def setup_logger(
    log_file: str = "logs/trading.log",
    level: str = "INFO",
    rotation: str = "10 MB",
    retention: str = "7 days"
) -> None:
    """配置日志系统"""
    # 确保日志目录存在
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 移除默认处理器
    logger.remove()
    
    # 添加控制台输出
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True
    )
    
    # 添加文件输出
    logger.add(
        log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=level,
        rotation=rotation,
        retention=retention,
        encoding="utf-8"
    )
    
    logger.info("日志系统初始化完成")


def get_logger(name: str):
    """获取指定名称的logger"""
    return logger.bind(name=name)
