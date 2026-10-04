"""岗位分析系统的内部包。

目前包含：
    jobanal.parsing   字段解析与清洗规则的唯一入口（纯标准库）
    jobanal.config    路径、常量与默认数据源

后续计划包：jobanal.store（SQLite 落库）、jobanal.web（Flask 应用工厂）。
"""

from __future__ import annotations

__all__ = ["parsing", "config", "__version__"]

__version__ = "0.2.0"
