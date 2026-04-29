"""Data models for MAX Bot API."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, List, Dict, Union


@dataclass
class User:
    """User info from MAX."""
    user_id: int
    name: str
    username: Optional[str] = None
    is_bot: bool = False
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "User":
        return cls(
            user_id=data.get("user_id", 0),
            name=data.get("name", ""),
            username=data.get("username"),
            is_bot=data.get("is_bot", False),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
        )


@dataclass
class Recipient:
    """Recipient of a message (chat or user)."""
    chat_id: int
    chat_type: str  # "dialog", "chat", "channel"
    user_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recipient":
        return cls(
            chat_id=data.get("chat_id", 0),
            chat_type=data.get("chat_type", "dialog"),
            user_id=data.get("user_id"),
        )


@dataclass
class MessageBody:
    """Body of a MAX message."""
    mid: str
    seq: int
    text: Optional[str] = None
    attachments: List[Any] = field(default_factory=list)
    reply_to: Optional[str] = None
    markups: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageBody":
        return cls(
            mid=data.get("mid", ""),
            seq=data.get("seq", 0),
            text=data.get("text"),
            attachments=data.get("attachments", []),
            reply_to=data.get("reply_to"),
            markups=data.get("markups", []),
        )


@dataclass
class Message:
    """A message in MAX."""
    sender: User
    recipient: Recipient
    timestamp: int
    body: MessageBody
    link: Optional[Dict[str, Any]] = None
    url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        return cls(
            sender=User.from_dict(data.get("sender", {})),
            recipient=Recipient.from_dict(data.get("recipient", {})),
            timestamp=data.get("timestamp", 0),
            body=MessageBody.from_dict(data.get("body", {})),
            link=data.get("link"),
            url=data.get("url"),
        )


@dataclass
class Update:
    """Base update object."""
    update_type: str
    timestamp: int
    raw: Dict[str, Any]

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)


@dataclass
class MessageCreatedUpdate(Update):
    """New message created."""
    message: Message

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageCreatedUpdate":
        return cls(
            update_type=data.get("update_type", "message_created"),
            timestamp=data.get("timestamp", 0),
            raw=data,
            message=Message.from_dict(data.get("message", {})),
        )


@dataclass
class MessageEditedUpdate(Update):
    """Message edited."""
    message: Message

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageEditedUpdate":
        return cls(
            update_type=data.get("update_type", "message_edited"),
            timestamp=data.get("timestamp", 0),
            raw=data,
            message=Message.from_dict(data.get("message", {})),
        )


@dataclass
class BotStartedUpdate(Update):
    """User started the bot."""
    chat_id: int
    user: User
    payload: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotStartedUpdate":
        return cls(
            update_type=data.get("update_type", "bot_started"),
            timestamp=data.get("timestamp", 0),
            raw=data,
            chat_id=data.get("chat_id", 0),
            user=User.from_dict(data.get("user", {})),
            payload=data.get("payload"),
        )


# All update types from Go client
UPDATE_TYPES = [
    "message_callback",
    "message_created",
    "message_removed",
    "message_edited",
    "bot_added",
    "bot_removed",
    "bot_stopped",
    "dialog_removed",
    "dialog_cleared",
    "user_added",
    "user_removed",
    "bot_started",
    "chat_title_changed",
]


def parse_update(data: Dict[str, Any]) -> Update:
    """Parse raw JSON update into typed Update object."""
    utype = data.get("update_type", "")

    if utype == "message_created":
        return MessageCreatedUpdate.from_dict(data)
    elif utype == "message_edited":
        return MessageEditedUpdate.from_dict(data)
    elif utype == "bot_started":
        return BotStartedUpdate.from_dict(data)
    else:
        return Update(update_type=utype, timestamp=data.get("timestamp", 0), raw=data)
