"""Pydantic 请求模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

OLD = "old"
NEW = "new"
VERSION_VALUES = (OLD, NEW)

LogType = Literal["start", "success"]


class SegmentIn(BaseModel):
    version: Literal["old", "new"]
    start: str = Field(..., description="整分钟，YYYY-MM-DDTHH:MM")
    end: str = Field(..., description="整分钟，末分钟的下一分钟，左闭右开")


class RoundIn(BaseModel):
    round_id: str = Field(..., min_length=1, max_length=64)
    segments: list[SegmentIn] = Field(..., min_length=2, max_length=2)


class ScheduleIn(BaseModel):
    rounds: list[RoundIn]


class LogEntryIn(BaseModel):
    txn_id: str = Field(..., min_length=1, max_length=128)
    ts: str
    type: LogType


class MinuteUpload(BaseModel):
    minute: str = Field(..., description="该日志包对应的整分钟 YYYY-MM-DDTHH:MM")
    entries: list[LogEntryIn]


class MigrationIn(BaseModel):
    """历史轮次迁入：一轮原格式排班 + 该轮观测窗口内的原格式分钟包。"""

    round: RoundIn
    minutes: list[MinuteUpload] = Field(default_factory=list)


class MigrationCommitIn(BaseModel):
    token: str = Field(..., min_length=1, max_length=128)
