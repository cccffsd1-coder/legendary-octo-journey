import datetime
import enum
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column, relationship
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Text, Enum, UniqueConstraint, Index, Float

DATABASE_URL = "mysql+aiomysql://root:root@localhost:3306/chats"

engine = create_async_engine(DATABASE_URL, echo=True)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    user_name = Column(String(150), nullable=False, unique=True)
    email = Column(String(255), nullable=True, unique=True)
    password = Column(String(500), nullable=False)
    status = Column(String(150), default="Online")
    numb = Column(Integer, default=0)
    is_deleted = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    avatar_url = Column(String(500), nullable=True)

    # Security & Sessions
    totp_secret = Column(String(100), nullable=True)
    is_2fa_enabled = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    banned_until = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    dark_mode = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships (lazy='select' for async compatibility)
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan", lazy="select")
    warnings = relationship("UserWarning", foreign_keys="UserWarning.user_id", back_populates="user", cascade="all, delete-orphan", lazy="select")
    sent_friend_requests = relationship("FriendRequest", foreign_keys="FriendRequest.sender_id", back_populates="sender", cascade="all, delete-orphan", lazy="select")
    received_friend_requests = relationship("FriendRequest", foreign_keys="FriendRequest.receiver_id", back_populates="receiver", cascade="all, delete-orphan", lazy="select")
    friends_sent = relationship("Friendship", foreign_keys="Friendship.user_id", back_populates="user", cascade="all, delete-orphan", lazy="select")
    friends_received = relationship("Friendship", foreign_keys="Friendship.friend_id", back_populates="friend", cascade="all, delete-orphan", lazy="select")
    group_memberships = relationship("GroupMember", back_populates="user", cascade="all, delete-orphan", lazy="select")
    activity_logs = relationship("ActivityLog", back_populates="user", cascade="all, delete-orphan", lazy="select")


class UserSession(Base):
    """User login sessions for multi-device management"""
    __tablename__ = 'user_sessions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    session_token = Column(String(500), nullable=False, unique=True)
    device_info = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_activity = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    
    user = relationship("User", back_populates="sessions", lazy="select")


class PasswordResetToken(Base):
    """Tokens for password reset"""
    __tablename__ = 'password_reset_tokens'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token = Column(String(500), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)


class EmailVerificationToken(Base):
    """Tokens for email verification"""
    __tablename__ = 'email_verification_tokens'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token = Column(String(500), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    verified = Column(Boolean, default=False)


class LoginAttempt(Base):
    """Track login attempts for rate limiting"""
    __tablename__ = 'login_attempts'
    
    id = Column(Integer, primary_key=True)
    username = Column(String(150), nullable=False, index=True)
    ip_address = Column(String(45), nullable=True)
    success = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


class ActivityLog(Base):
    """User activity logging for admin panel"""
    __tablename__ = 'activity_logs'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    action = Column(String(100), nullable=False)
    details = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    
    user = relationship("User", back_populates="activity_logs", lazy="select")


class UserWarning(Base):
    """Admin warnings for users"""
    __tablename__ = 'user_warnings'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    admin_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    
    user = relationship("User", foreign_keys=[user_id], back_populates="warnings", lazy="select")
    admin = relationship("User", foreign_keys=[admin_id], lazy="select")


class Friendship(Base):
    """Friend relationships"""
    __tablename__ = 'friendships'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    friend_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'friend_id', name='uq_friendship'),
    )
    
    user = relationship("User", foreign_keys=[user_id], back_populates="friends_sent", lazy="select")
    friend = relationship("User", foreign_keys=[friend_id], back_populates="friends_received", lazy="select")


class FriendRequest(Base):
    """Pending friend requests"""
    __tablename__ = 'friend_requests'
    
    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    receiver_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String(20), default="pending")  # pending, accepted, rejected
    
    __table_args__ = (
        UniqueConstraint('sender_id', 'receiver_id', name='uq_friend_request'),
    )
    
    sender = relationship("User", foreign_keys=[sender_id], back_populates="sent_friend_requests", lazy="select")
    receiver = relationship("User", foreign_keys=[receiver_id], back_populates="received_friend_requests", lazy="select")


class Message(Base):
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True)
    sender_name = Column(String(150), nullable=False, index=True)
    receiver_name = Column(String(150), nullable=False, index=True)

    text = Column(String(2000), nullable=True)

    file_path = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)
    file_type = Column(String(50), nullable=True)  # image, audio, document
    
    # Message features
    reply_to_id = Column(Integer, ForeignKey('messages.id', ondelete='SET NULL'), nullable=True)
    edited_at = Column(DateTime, nullable=True)
    is_deleted = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False, index=True)
    delivery_status = Column(String(20), default="sent")  # sent, delivered, read
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow
    )
    
    # Relationships
    reactions = relationship("MessageReaction", back_populates="message", cascade="all, delete-orphan", lazy="select")
    reply_to = relationship("Message", remote_side=[id], backref="replies", lazy="select")


class MessageReaction(Base):
    """Emoji reactions on messages"""
    __tablename__ = 'message_reactions'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    user_name = Column(String(150), nullable=False)
    emoji = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('message_id', 'user_name', 'emoji', name='uq_reaction'),
    )
    
    message = relationship("Message", back_populates="reactions", lazy="select")


class Group(Base):
    """Group chats"""
    __tablename__ = 'groups'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    avatar_url = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan", lazy="select")
    messages = relationship("GroupMessage", back_populates="group", cascade="all, delete-orphan", lazy="select")


class GroupMember(Base):
    """Group membership"""
    __tablename__ = 'group_members'
    
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('groups.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    role = Column(String(20), default="member")  # owner, admin, member
    joined_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_banned = Column(Boolean, default=False)
    
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', name='uq_group_member'),
    )
    
    group = relationship("Group", back_populates="members", lazy="select")
    user = relationship("User", back_populates="group_memberships", lazy="select")


class GroupMessage(Base):
    """Messages in group chats"""
    __tablename__ = 'group_messages'
    
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('groups.id', ondelete='CASCADE'), nullable=False)
    sender_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    text = Column(String(2000), nullable=True)
    file_path = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)
    file_type = Column(String(50), nullable=True)
    reply_to_id = Column(Integer, ForeignKey('group_messages.id', ondelete='SET NULL'), nullable=True)
    edited_at = Column(DateTime, nullable=True)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    
    group = relationship("Group", back_populates="messages", lazy="select")
    sender = relationship("User", lazy="select")
    reply_to = relationship("GroupMessage", remote_side=[id], backref="replies", lazy="select")


class PushNotificationToken(Base):
    """Push notification tokens for web/mobile"""
    __tablename__ = 'push_notification_tokens'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token = Column(String(500), nullable=False)
    device_type = Column(String(50), nullable=True)  # web, android, ios
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'token', name='uq_push_token'),
    )


class ProfanityFilter(Base):
    """Profanity words for content moderation"""
    __tablename__ = 'profanity_filter'
    
    id = Column(Integer, primary_key=True)
    word = Column(String(100), nullable=False, unique=True)
    replacement = Column(String(10), default="****")
    is_active = Column(Boolean, default=True)


class Report(Base):
    __tablename__ = 'reports'
    id = Column(Integer, primary_key=True)
    reporter_name = Column(String(150), nullable=False)
    reported_name = Column(String(150), nullable=False)
    comment = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String(20), default="pending")  # pending, reviewed, resolved


# ==================== NOWE MODELE - WSZYSTKIE FUNKCJE ====================

class TwoFABackupCode(Base):
    """Backup codes for 2FA"""
    __tablename__ = '2fa_backup_codes'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    code_hash = Column(String(500), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class LoginHistory(Base):
    """Login history for security tracking"""
    __tablename__ = 'login_history'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    ip_address = Column(String(45), nullable=True)
    device_info = Column(String(255), nullable=True)
    location = Column(String(255), nullable=True)  # Geolocation
    success = Column(Boolean, default=True)
    failure_reason = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    
    user = relationship("User", lazy="select")


class BlockedUser(Base):
    """Blocked users list"""
    __tablename__ = 'blocked_users'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    blocked_user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'blocked_user_id', name='uq_blocked'),
    )
    
    user = relationship("User", foreign_keys=[user_id], lazy="select", backref="blocked_list")
    blocked_user = relationship("User", foreign_keys=[blocked_user_id], lazy="select")


class PinnedMessage(Base):
    """Pinned messages in chat"""
    __tablename__ = 'pinned_messages'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    pinned_by = Column(String(150), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    message = relationship("Message", lazy="select")
    user = relationship("User", lazy="select")


class StarredMessage(Base):
    """Starred/favorited messages"""
    __tablename__ = 'starred_messages'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'message_id', name='uq_starred'),
    )
    
    message = relationship("Message", lazy="select")
    user = relationship("User", lazy="select")


class VoiceMessage(Base):
    """Voice message metadata"""
    __tablename__ = 'voice_messages'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    duration = Column(Integer, nullable=False)  # Duration in seconds
    waveform = Column(Text, nullable=True)  # Audio waveform data
    
    message = relationship("Message", lazy="select")


class VideoMessage(Base):
    """Video message metadata"""
    __tablename__ = 'video_messages'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    duration = Column(Integer, nullable=True)  # Duration in seconds
    thumbnail_path = Column(String(500), nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    
    message = relationship("Message", lazy="select")


class Poll(Base):
    """Polls in messages"""
    __tablename__ = 'polls'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    question = Column(String(500), nullable=False)
    multiple_choice = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    ends_at = Column(DateTime, nullable=True)
    
    message = relationship("Message", lazy="select")
    options = relationship("PollOption", back_populates="poll", cascade="all, delete-orphan", lazy="select")
    votes = relationship("PollVote", back_populates="poll", cascade="all, delete-orphan", lazy="select")


class PollOption(Base):
    """Poll options"""
    __tablename__ = 'poll_options'
    
    id = Column(Integer, primary_key=True)
    poll_id = Column(Integer, ForeignKey('polls.id', ondelete='CASCADE'), nullable=False)
    text = Column(String(200), nullable=False)
    vote_count = Column(Integer, default=0)
    
    poll = relationship("Poll", back_populates="options")


class PollVote(Base):
    """Poll votes"""
    __tablename__ = 'poll_votes'
    
    id = Column(Integer, primary_key=True)
    poll_id = Column(Integer, ForeignKey('polls.id', ondelete='CASCADE'), nullable=False)
    option_id = Column(Integer, ForeignKey('poll_options.id', ondelete='CASCADE'), nullable=False)
    voter_name = Column(String(150), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    poll = relationship("Poll", back_populates="votes")
    option = relationship("PollOption", lazy="select")


class Sticker(Base):
    """Sticker packs"""
    __tablename__ = 'stickers'
    
    id = Column(Integer, primary_key=True)
    pack_name = Column(String(100), nullable=False)
    sticker_url = Column(String(500), nullable=False)
    emoji = Column(String(50), nullable=True)
    is_premium = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class UserSticker(Base):
    """User's sticker collection"""
    __tablename__ = 'user_stickers'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    sticker_id = Column(Integer, ForeignKey('stickers.id', ondelete='CASCADE'), nullable=False)
    usage_count = Column(Integer, default=0)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'sticker_id', name='uq_user_sticker'),
    )
    
    user = relationship("User", lazy="select")
    sticker = relationship("Sticker", lazy="select")


class ChatTheme(Base):
    """Chat themes"""
    __tablename__ = 'chat_themes'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    primary_color = Column(String(20), nullable=False)
    secondary_color = Column(String(20), nullable=True)
    background_url = Column(String(500), nullable=True)
    is_dark = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)


class UserTheme(Base):
    """User's selected theme"""
    __tablename__ = 'user_themes'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    theme_id = Column(Integer, ForeignKey('chat_themes.id', ondelete='SET NULL'), nullable=True)
    custom_primary = Column(String(20), nullable=True)
    custom_secondary = Column(String(20), nullable=True)
    
    user = relationship("User", lazy="select")
    theme = relationship("ChatTheme", lazy="select")


class MutedChat(Base):
    """Muted chats"""
    __tablename__ = 'muted_chats'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=False)  # Username or group ID
    muted_until = Column(DateTime, nullable=True)  # NULL = permanently
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'chat_with', name='uq_muted_chat'),
    )
    
    user = relationship("User", lazy="select")


class MessageEdit(Base):
    """Message edit history"""
    __tablename__ = 'message_edits'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    old_text = Column(String(2000), nullable=False)
    edited_at = Column(DateTime, default=datetime.datetime.utcnow)
    edited_by = Column(String(150), nullable=False)
    
    message = relationship("Message", lazy="select")


class ChatWallpaper(Base):
    """Chat wallpapers"""
    __tablename__ = 'chat_wallpapers'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=True)  # NULL = default for all
    wallpaper_url = Column(String(500), nullable=False)
    wallpaper_type = Column(String(20), default="image")  # image, color, gradient
    
    user = relationship("User", lazy="select")


class AutoDeleteSetting(Base):
    """Auto-delete message settings"""
    __tablename__ = 'auto_delete_settings'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=True)  # NULL = global setting
    delete_after_hours = Column(Integer, nullable=False)  # 24, 7 days, 30 days, etc.
    enabled = Column(Boolean, default=False)
    
    user = relationship("User", lazy="select")


class UserLanguage(Base):
    """User language preference"""
    __tablename__ = 'user_languages'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    language = Column(String(10), default="pl")  # pl, en, uk, etc.
    
    user = relationship("User", lazy="select")


class UserStatistic(Base):
    """User statistics"""
    __tablename__ = 'user_statistics'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    messages_sent = Column(Integer, default=0)
    messages_received = Column(Integer, default=0)
    files_sent = Column(Integer, default=0)
    voice_messages_sent = Column(Integer, default=0)
    stickers_sent = Column(Integer, default=0)
    polls_created = Column(Integer, default=0)
    last_stats_update = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class BotIntegration(Base):
    """Bot integrations"""
    __tablename__ = 'bot_integrations'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    api_key = Column(String(500), nullable=False)
    webhook_url = Column(String(500), nullable=True)
    enabled = Column(Boolean, default=True)
    config = Column(Text, nullable=True)  # JSON config
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FocusMode(Base):
    """Focus mode settings"""
    __tablename__ = 'focus_modes'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    enabled = Column(Boolean, default=False)
    hide_sidebar = Column(Boolean, default=False)
    hide_notifications = Column(Boolean, default=False)
    quiet_hours_start = Column(Integer, nullable=True)  # Hour 0-23
    quiet_hours_end = Column(Integer, nullable=True)
    
    user = relationship("User", lazy="select")


class KeyboardShortcut(Base):
    """Custom keyboard shortcuts"""
    __tablename__ = 'keyboard_shortcuts'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    action = Column(String(100), nullable=False)
    shortcut = Column(String(50), nullable=False)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'action', name='uq_shortcut'),
    )
    
    user = relationship("User", lazy="select")


class QRCodeData(Base):
    """Generated QR codes"""
    __tablename__ = 'qr_codes'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    data = Column(Text, nullable=False)
    qr_image_path = Column(String(500), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class FileStorage(Base):
    """Cloud file storage"""
    __tablename__ = 'file_storage'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=False)  # Bytes
    file_type = Column(String(50), nullable=True)
    is_compressed = Column(Boolean, default=False)
    original_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


# ==================== FAZA 1: ORGANIZACJA CZATÓW ====================

class ChatFolder(Base):
    """Chat folders for organization"""
    __tablename__ = 'chat_folders'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String(100), nullable=False)
    icon = Column(String(50), default="📁")
    color = Column(String(20), default="#3a5bd9")
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")
    chats = relationship("FolderChat", back_populates="folder", cascade="all, delete-orphan", lazy="select")


class FolderChat(Base):
    """Chats assigned to folders"""
    __tablename__ = 'folder_chats'
    
    id = Column(Integer, primary_key=True)
    folder_id = Column(Integer, ForeignKey('chat_folders.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=False)  # Username or group ID
    position = Column(Integer, default=0)
    
    __table_args__ = (
        UniqueConstraint('folder_id', 'chat_with', name='uq_folder_chat'),
    )
    
    folder = relationship("ChatFolder", back_populates="chats")


class ArchivedChat(Base):
    """Archived chats"""
    __tablename__ = 'archived_chats'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=False)
    archived_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'chat_with', name='uq_archived_chat'),
    )
    
    user = relationship("User", lazy="select")


class PinnedChat(Base):
    """Pinned chats at top of list"""
    __tablename__ = 'pinned_chats'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=False)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'chat_with', name='uq_pinned_chat'),
    )
    
    user = relationship("User", lazy="select")


# ==================== FAZA 2: WIADOMOŚCI CZASOWE ====================

class ScheduledMessage(Base):
    """Messages scheduled for future delivery"""
    __tablename__ = 'scheduled_messages'
    
    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    sender_name = Column(String(150), nullable=False)
    receiver_name = Column(String(150), nullable=False)
    text = Column(String(2000), nullable=True)
    file_path = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    sender = relationship("User", lazy="select")


class DisappearingMessage(Base):
    """Disappearing messages settings"""
    __tablename__ = 'disappearing_messages'
    
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    delete_after_seconds = Column(Integer, nullable=False)
    delete_at = Column(DateTime, nullable=True)
    deleted = Column(Boolean, default=False)
    
    user = relationship("User", lazy="select")
    message = relationship("Message", lazy="select")


# ==================== FAZA 3: WYSZUKIWANIE ====================

class SearchIndex(Base):
    """Full-text search index for messages"""
    __tablename__ = 'search_index'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    content = Column(Text, nullable=False)  # Searchable content
    sender_name = Column(String(150), nullable=False, index=True)
    receiver_name = Column(String(150), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    
    message = relationship("Message", lazy="select")


class SearchHistory(Base):
    """User search history"""
    __tablename__ = 'search_history'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    query = Column(String(500), nullable=False)
    results_count = Column(Integer, default=0)
    searched_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


# ==================== FAZA 4: MEDIA ====================

class PhotoEdit(Base):
    """Photo edits before sending"""
    __tablename__ = 'photo_edits'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    filter_name = Column(String(50), nullable=True)
    crop_data = Column(Text, nullable=True)  # JSON crop coordinates
    draw_data = Column(Text, nullable=True)  # JSON draw strokes
    brightness = Column(Integer, default=0)  # -100 to 100
    contrast = Column(Integer, default=0)  # -100 to 100
    saturation = Column(Integer, default=0)  # -100 to 100
    
    message = relationship("Message", lazy="select")


class VoiceTranscription(Base):
    """Voice message transcriptions"""
    __tablename__ = 'voice_transcriptions'
    
    id = Column(Integer, primary_key=True)
    voice_message_id = Column(Integer, ForeignKey('voice_messages.id', ondelete='CASCADE'), nullable=False)
    transcription = Column(Text, nullable=False)
    language = Column(String(10), default="pl")
    confidence = Column(Float, default=1.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    voice_message = relationship("VoiceMessage", lazy="select")


class MediaGallery(Base):
    """Shared media gallery for chats"""
    __tablename__ = 'media_galleries'
    
    id = Column(Integer, primary_key=True)
    chat_identifier = Column(String(150), nullable=False)  # username or group_id
    media_type = Column(String(20), nullable=False)  # photo, video, audio, file, link
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    file_url = Column(String(500), nullable=False)
    thumbnail_url = Column(String(500), nullable=True)
    file_size = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    
    message = relationship("Message", lazy="select")


# ==================== FAZA 5: SPOŁECZNOŚCIOWE ====================

class UserProfile(Base):
    """Extended user profile"""
    __tablename__ = 'user_profiles'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    bio = Column(String(500), nullable=True)
    cover_photo_url = Column(String(500), nullable=True)
    website = Column(String(255), nullable=True)
    location = Column(String(100), nullable=True)
    birthdate = Column(DateTime, nullable=True)
    is_public = Column(Boolean, default=False)
    instagram = Column(String(100), nullable=True)
    twitter = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class UserStory(Base):
    """24h disappearing stories"""
    __tablename__ = 'user_stories'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    media_type = Column(String(20), nullable=False)  # photo, video, text
    media_url = Column(String(500), nullable=False)
    thumbnail_url = Column(String(500), nullable=True)
    text_content = Column(String(500), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")
    views = relationship("StoryView", back_populates="story", cascade="all, delete-orphan", lazy="select")


class StoryView(Base):
    """Story view tracking"""
    __tablename__ = 'story_views'
    
    id = Column(Integer, primary_key=True)
    story_id = Column(Integer, ForeignKey('user_stories.id', ondelete='CASCADE'), nullable=False)
    viewer_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    viewed_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('story_id', 'viewer_id', name='uq_story_view'),
    )
    
    story = relationship("UserStory", back_populates="views")
    viewer = relationship("User", lazy="select")


class StoryReply(Base):
    """Replies to stories"""
    __tablename__ = 'story_replies'
    
    id = Column(Integer, primary_key=True)
    story_id = Column(Integer, ForeignKey('user_stories.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    text = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    story = relationship("UserStory", lazy="select")
    user = relationship("User", lazy="select")


# ==================== FAZA 6: AI FUNKCJE ====================

class SmartReply(Base):
    """AI smart reply suggestions"""
    __tablename__ = 'smart_replies'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    suggestion = Column(String(200), nullable=False)
    confidence = Column(Float, default=0.0)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class Translation(Base):
    """Message translations"""
    __tablename__ = 'translations'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    original_text = Column(String(2000), nullable=False)
    translated_text = Column(String(2000), nullable=False)
    source_language = Column(String(10), nullable=True)
    target_language = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    message = relationship("Message", lazy="select")


class ChatSummary(Base):
    """AI-generated chat summaries"""
    __tablename__ = 'chat_summaries'
    
    id = Column(Integer, primary_key=True)
    chat_identifier = Column(String(150), nullable=False)
    summary = Column(Text, nullable=False)
    date_from = Column(DateTime, nullable=False)
    date_to = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ==================== FAZA 7: PRODUKTYWNOŚĆ ====================

class ChatTask(Base):
    """Tasks in chat"""
    __tablename__ = 'chat_tasks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=True)  # Associated chat
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(DateTime, nullable=True)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class ChatNote(Base):
    """Notes in chat"""
    __tablename__ = 'chat_notes'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    color = Column(String(20), default="#ffffff")
    is_pinned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class Bookmark(Base):
    """Bookmarked messages"""
    __tablename__ = 'bookmarks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    note = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('user_id', 'message_id', name='uq_bookmark'),
    )
    
    user = relationship("User", lazy="select")
    message = relationship("Message", lazy="select")


# ==================== FAZA 8: BEZPIECZEŃSTWO ====================

class SecretChat(Base):
    """E2E encrypted secret chats"""
    __tablename__ = 'secret_chats'
    
    id = Column(Integer, primary_key=True)
    user1_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    user2_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    encryption_key = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    
    __table_args__ = (
        UniqueConstraint('user1_id', 'user2_id', name='uq_secret_chat'),
    )


class AppLock(Base):
    """App lock settings"""
    __tablename__ = 'app_locks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    pin_hash = Column(String(500), nullable=True)
    biometric_enabled = Column(Boolean, default=False)
    lock_timeout_minutes = Column(Integer, default=5)
    enabled = Column(Boolean, default=False)
    
    user = relationship("User", lazy="select")


class ScreenshotLog(Base):
    """Screenshot detection logs"""
    __tablename__ = 'screenshot_logs'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    chat_with = Column(String(150), nullable=True)
    detected_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


# ==================== FAZA 9: FUN ====================

class ChatGame(Base):
    """Games in chat"""
    __tablename__ = 'chat_games'
    
    id = Column(Integer, primary_key=True)
    game_type = Column(String(50), nullable=False)  # chess, tictactoe, quiz
    player1_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    player2_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    state = Column(Text, nullable=False)  # JSON game state
    winner_id = Column(Integer, nullable=True)
    status = Column(String(20), default="playing")  # playing, finished
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class CustomEmoji(Base):
    """Custom user emojis"""
    __tablename__ = 'custom_emojis'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String(50), nullable=False)
    image_url = Column(String(500), nullable=False)
    usage_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


class MessageEffect(Base):
    """Special message effects"""
    __tablename__ = 'message_effects'
    
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False)
    effect_type = Column(String(50), nullable=False)  # confetti, fireworks, hearts
    triggered = Column(Boolean, default=False)
    
    message = relationship("Message", lazy="select")


# ==================== FAZA 10: INTEGRACJE ====================

class CloudBackup(Base):
    """Cloud backup settings"""
    __tablename__ = 'cloud_backups'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    provider = Column(String(20), nullable=False)  # google, dropbox, onedrive
    access_token = Column(String(500), nullable=False)
    last_backup = Column(DateTime, nullable=True)
    auto_backup = Column(Boolean, default=True)
    backup_frequency = Column(String(20), default="daily")  # daily, weekly, monthly
    
    user = relationship("User", lazy="select")


class EmailNotification(Base):
    """Email notification settings"""
    __tablename__ = 'email_notifications'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    email = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=False)
    notify_on_mention = Column(Boolean, default=True)
    notify_on_message = Column(Boolean, default=False)
    digest_frequency = Column(String(20), default="daily")  # never, daily, weekly
    
    user = relationship("User", lazy="select")


class WebhookIntegration(Base):
    """External webhook integrations"""
    __tablename__ = 'webhook_integrations'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name = Column(String(100), nullable=False)
    webhook_url = Column(String(500), nullable=False)
    events = Column(Text, nullable=False)  # JSON array
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", lazy="select")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)