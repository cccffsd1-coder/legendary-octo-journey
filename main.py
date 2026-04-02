import json
import os
import uuid
import qrcode
import io
import base64
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

from starlette.responses import RedirectResponse

from sqlalchemy import select, or_, and_, func, delete, update
from sqlalchemy.exc import IntegrityError

from urllib.parse import unquote, urlencode
from contextlib import asynccontextmanager

from db import (
    engine, Base, AsyncSessionLocal, User, Message, Report,
    UserSession, PasswordResetToken, EmailVerificationToken, LoginAttempt,
    ActivityLog, UserWarning, Friendship, FriendRequest, MessageReaction,
    Group, GroupMember, GroupMessage, PushNotificationToken, ProfanityFilter,
    TwoFABackupCode, LoginHistory, BlockedUser, PinnedMessage, StarredMessage,
    VoiceMessage, VideoMessage, Poll, PollOption, PollVote, Sticker, UserSticker,
    ChatTheme, UserTheme, MutedChat, MessageEdit, ChatWallpaper, AutoDeleteSetting,
    UserLanguage, UserStatistic, BotIntegration, FocusMode, KeyboardShortcut,
    QRCodeData, FileStorage,
    # FAZA 1-10: Wszystkie nowe modele
    ChatFolder, FolderChat, ArchivedChat, PinnedChat,
    ScheduledMessage, DisappearingMessage,
    SearchIndex, SearchHistory,
    PhotoEdit, VoiceTranscription, MediaGallery,
    UserProfile, UserStory, StoryView, StoryReply,
    SmartReply, Translation, ChatSummary,
    ChatTask, ChatNote, Bookmark,
    SecretChat, AppLock, ScreenshotLog,
    ChatGame, CustomEmoji, MessageEffect,
    CloudBackup, EmailNotification, WebhookIntegration
)
from sqlalchemy.orm import selectinload
from security_utils import (
    hash_password, verify_password, generate_totp_secret, get_totp_uri,
    verify_totp, generate_reset_token, generate_verification_token,
    create_access_token, decode_access_token, hash_token,
    verify_password_strength, censor_profanity, RESET_TOKEN_EXPIRE_HOURS
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(lifespan=lifespan)

# Додаємо Middleware для сесій (секретний ключ має бути в .env)
SECRET_KEY = "super-secret-key-that-should-be-in-env"
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket
        await self.broadcast_status(username, "online")

    async def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
            await self.broadcast_status(username, "offline")

    async def broadcast_status(self, username: str, status: str):
        payload = {
            "type": "status",
            "username": username,
            "status": status
        }
        for connection in self.active_connections.values():
            try:
                await connection.send_json(payload)
            except:
                pass

    async def send_personal_message(self, message: dict, receiver: str):
        if receiver in self.active_connections:
            await self.active_connections[receiver].send_json(message)


manager = ConnectionManager()


@app.get("/notifications")
async def get_notifications(request: Request):
    user = request.session.get("user_name")
    if not user:
        return {}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Message.sender_name, func.count(Message.id))
            .where(
                Message.receiver_name == user,
                Message.is_read == False
            )
            .group_by(Message.sender_name)
        )
        rows = result.all()

    return {sender: count for sender, count in rows}

@app.get("/api/notifications/all")
async def get_all_notifications(request: Request):
    """Get all notifications including messages, mentions, system"""
    user = request.session.get("user_name")
    if not user:
        return {"notifications": [], "total": 0}

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user))
        current_user = result.scalars().first()
        
        notifications = []
        
        # Unread messages
        result = await session.execute(
            select(Message)
            .where(Message.receiver_name == user, Message.is_read == False)
            .order_by(Message.created_at.desc())
            .limit(50)
        )
        messages = result.scalars().all()
        
        for msg in messages:
            notifications.append({
                "id": f"msg_{msg.id}",
                "type": "message",
                "sender": msg.sender_name,
                "text": msg.text[:100] if msg.text else "[file]",
                "time": msg.created_at.isoformat(),
                "read": False,
                "icon": "💬"
            })
        
        # Mentions (search for @username in messages)
        result = await session.execute(
            select(Message)
            .where(
                Message.receiver_name == user,
                Message.text.like(f"%@{user}%")
            )
            .order_by(Message.created_at.desc())
            .limit(20)
        )
        mentions = result.scalars().all()
        
        for msg in mentions:
            if not any(n["id"] == f"msg_{msg.id}" for n in notifications):
                notifications.append({
                    "id": f"mention_{msg.id}",
                    "type": "mention",
                    "sender": msg.sender_name,
                    "text": msg.text[:100],
                    "time": msg.created_at.isoformat(),
                    "read": False,
                    "icon": "🔔"
                })
        
        # System notifications (warnings, bans, etc.)
        result = await session.execute(
            select(UserWarning)
            .where(UserWarning.user_id == current_user.id, UserWarning.is_active == True)
            .order_by(UserWarning.created_at.desc())
        )
        warnings = result.scalars().all()
        
        for warn in warnings:
            notifications.append({
                "id": f"warn_{warn.id}",
                "type": "warning",
                "sender": "Admin",
                "text": f"Ostrzeżenie: {warn.reason}",
                "time": warn.created_at.isoformat(),
                "read": False,
                "icon": "⚠️"
            })
        
        # Sort by time
        notifications.sort(key=lambda x: x["time"], reverse=True)
        
        return {
            "notifications": notifications,
            "total": len([n for n in notifications if not n["read"]]),
            "unread_count": len([n for n in notifications if not n["read"]])
        }

@app.post("/api/notifications/mark-read")
async def mark_notifications_read(request: Request, notification_ids: str = Form(None)):
    """Mark notifications as read"""
    user = request.session.get("user_name")
    if not user:
        return {"ok": False}

    import json
    ids = json.loads(notification_ids) if notification_ids else []
    
    async with AsyncSessionLocal() as session:
        # Mark messages as read
        for nid in ids:
            if nid.startswith("msg_"):
                msg_id = int(nid.replace("msg_", ""))
                result = await session.execute(
                    select(Message).where(Message.id == msg_id, Message.receiver_name == user)
                )
                msg = result.scalars().first()
                if msg:
                    msg.is_read = True
        
        await session.commit()
    
    return {"ok": True}

@app.post("/api/notifications/mark-all-read")
async def mark_all_notifications_read(request: Request):
    """Mark all notifications as read"""
    user = request.session.get("user_name")
    if not user:
        return {"ok": False}

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Message)
            .where(Message.receiver_name == user, Message.is_read == False)
            .values(is_read=True)
        )
        await session.commit()
    
    return {"ok": True}

@app.post("/read/{sender}")
async def read_messages(sender: str, request: Request):
    user = request.session.get("user_name")
    if not user:
        return {"ok": False}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Message).where(
                Message.sender_name == sender,
                Message.receiver_name == user,
                Message.is_read == False
            )
        )
        messages = result.scalars().all()

        for msg in messages:
            msg.is_read = True

        await session.commit()

    return {"ok": True}


@app.get('/')
async def landing(request: Request):
    user_name = request.session.get("user_name")
    return templates.TemplateResponse(
        'landing.html',
        {
            'request': request,
            'current_user': user_name
        }
    )

@app.get('/chat')
async def index(request: Request):
    user_name = request.session.get("user_name")

    if not user_name:
        return RedirectResponse(url='/login')

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        current_user_obj = result.scalars().first()
        
        result = await session.execute(select(User).where(User.user_name != user_name))
        users = result.scalars().all()
        
    users_with_status = []
    for u in users:
        u_dict = {
            "user_name": u.user_name,
            "avatar_url": u.avatar_url,
            "status": "online" if u.user_name in manager.active_connections else "offline"
        }
        users_with_status.append(u_dict)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "session": request.session,
            "users": users_with_status,
            "current_user": user_name,
            "current_user_is_admin": User.is_admin if User else False,  # Teraz 'user' istnieje
        }
    )


@app.get('/messages/{other_user}')
async def get_messages(other_user: str, request: Request):
    current_user = request.session.get("user_name")
    if not current_user:
        return []

    async with AsyncSessionLocal() as session:
        query = select(Message).options(
            selectinload(Message.reply_to)
        ).where(
            or_(
                and_(Message.sender_name == current_user, Message.receiver_name == other_user),
                and_(Message.sender_name == other_user, Message.receiver_name == current_user)
            )
        ).order_by(Message.created_at.asc())

        result = await session.execute(query)
        messages = result.scalars().all()

    return [
        {
            "id": m.id,
            "sender": m.sender_name,
            "text": m.text,
            "file": m.file_name,
            "file_url": m.file_path,
            "reply_to": {"text": m.reply_to.text} if m.reply_to and m.reply_to.text else None
        }
        for m in messages
    ]


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...), to: str = Form(...)):
    user = request.session.get("user_name")
    file_path = f"uploads/{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())

    async with AsyncSessionLocal() as session:
        new_msg = Message(sender=user, receiver=to, file_name=file.filename, file_url=f"/{file_path}")
        session.add(new_msg)
        await session.commit()

    payload = {"type": "message", "sender": user, "file": file.filename, "file_url": f"/{file_path}", "to": to}
    await manager.send_personal_message(payload, to)
    await manager.send_personal_message(payload, user)
    return {"status": "ok"}


@app.websocket('/ws/{username}')
async def websocket_endpoint(websocket: WebSocket, username: str):
    clean_name = unquote(username)
    await manager.connect(clean_name, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            msg_data = json.loads(data)

            receiver = msg_data.get('to')
            text = msg_data.get('text')
            reply_to_id = msg_data.get('reply_to_id')

            if receiver and text:
                async with AsyncSessionLocal() as session:
                    new_msg = Message(
                        sender_name=clean_name,
                        receiver_name=receiver,
                        text=text,
                        reply_to_id=reply_to_id if reply_to_id else None
                    )

                    session.add(new_msg)
                    await session.commit()
                    await session.refresh(new_msg)

                    # Get reply_to text if exists
                    reply_to_text = None
                    if reply_to_id:
                        reply_msg = await session.get(Message, reply_to_id)
                        if reply_msg and reply_msg.text:
                            reply_to_text = reply_msg.text

                payload = {
                    "type": "message",
                    "sender": clean_name,
                    "to": receiver,
                    "text": text,
                    "id": new_msg.id,
                    "reply_to": {"text": reply_to_text} if reply_to_text else None
                }

                # Dispatch event to bots
                await bot_manager.dispatch_event("message.sent", {
                    "sender": clean_name,
                    "receiver": receiver,
                    "text": text,
                    "id": new_msg.id
                })

                await manager.send_personal_message(payload, receiver)
                await manager.send_personal_message(payload, clean_name)

    except WebSocketDisconnect:
        await manager.disconnect(clean_name)


@app.get('/login')
async def login_page(request: Request):
    return templates.TemplateResponse('auth.html', {'request': request, 'title': 'Logowanie'})

@app.post('/login')
async def login_user(request: Request, username: str = Form(...), password: str = Form(...)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()

    if user and verify_password(password, user.password):
        request.session["user_name"] = username
        return RedirectResponse(url='/chat', status_code=303)
    
    return templates.TemplateResponse('auth.html', {
        'request': request, 
        'title': 'Logowanie', 
        'error': 'Błędne dane logowania'
    })


@app.get('/register')
async def register_page(request: Request):
    return templates.TemplateResponse('auth.html', {'request': request, 'title': 'Rejestracja'})

@app.post('/register')
async def register_user(request: Request, username: str = Form(...), password: str = Form(...)):
    if len(password) < 6:
        return templates.TemplateResponse('auth.html', {
            'request': request, 
            'title': 'Rejestracja', 
            'error': 'Hasło musi mieć co najmniej 6 znaków'
        })

    async with AsyncSessionLocal() as session:
        try:
            hashed = hash_password(password)
            user = User(user_name=username, password=hashed)
            session.add(user)
            await session.commit()
            return RedirectResponse(url='/login', status_code=303)
        except IntegrityError:
            await session.rollback()
            return templates.TemplateResponse('auth.html', {
                'request': request, 
                'title': 'Rejestracja', 
                'error': 'Użytkownik istnieje'
            })


@app.get('/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url='/')


@app.get('/settings')
async def settings_page(request: Request):
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()

    return templates.TemplateResponse(
        'settings_enhanced.html',
        {
            'request': request,
            'current_user': user_name,
            'user': user
        }
    )

@app.post('/settings')
async def update_settings(
    request: Request,
    status: str = Form(None),
    new_password: str = Form(None),
    avatar: UploadFile = File(None)
):
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')

    message = "Ustawienia zaktualizowane"
    success = True

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()

        if user:
            if status is not None:
                user.status = status

            if new_password and len(new_password.strip()) > 0:
                if len(new_password) < 6:
                    message = "Hasło musi mieć co najmniej 6 znaków"
                    success = False
                else:
                    user.password = hash_password(new_password)

            if success and avatar and avatar.filename:
                file_ext = avatar.filename.split('.')[-1]
                filename = f"{user.id}_{uuid.uuid4()}.{file_ext}"
                file_path = f"{UPLOAD_DIR}/{filename}"
                with open(file_path, "wb") as buffer:
                    buffer.write(await avatar.read())
                user.avatar_url = f"/uploads/{filename}"

            if success:
                await session.commit()
        else:
            message = "Nie znaleziono użytkownika"
            success = False
            
    return templates.TemplateResponse(
        'settings.html',
        {
            'request': request,
            'current_user': user_name,
            'user': user,
            'message': message,
            'success': success
        }
    )


@app.post('/report')
async def create_report(
    request: Request,
    reported_name: str = Form(...),
    comment: str = Form(...)
):
    reporter_name = request.session.get("user_name")
    if not reporter_name:
        raise HTTPException(status_code=401, detail="Not logged in")

    async with AsyncSessionLocal() as session:
        new_report = Report(
            reporter_name=reporter_name,
            reported_name=reported_name,
            comment=comment
        )
        session.add(new_report)
        await session.commit()

    return {"ok": True}

@app.get('/admin')
async def admin_panel(request: Request):
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()

        if not user or not user.is_admin:
            return "Dostęp zabroniony"

        # Pobierz skargi
        reports_res = await session.execute(select(Report).order_by(Report.created_at.desc()))
        reports = reports_res.scalars().all()

        # Pobierz wszystkich użytkowników
        users_res = await session.execute(select(User))
        all_users = users_res.scalars().all()

    return templates.TemplateResponse(
        'admin.html',
        {
            'request': request,
            'current_user': user_name,
            'reports': reports,
            'users': all_users
        }
    )

@app.get('/bots')
async def bots_page(request: Request):
    """Bot management page"""
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()

        if not user or not user.is_admin:
            return RedirectResponse(url='/admin')

    return templates.TemplateResponse(
        'bots.html',
        {
            'request': request,
            'current_user': user_name
        }
    )


@app.delete("/message/{message_id}")
async def delete_message(message_id: int, request: Request):
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Message).where(Message.id == message_id)
        )
        msg = result.scalars().first()

        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")

        if msg.sender_name != user_name:
            raise HTTPException(status_code=403, detail="Not your message")

        await session.delete(msg)
        await session.commit()

    return {"ok": True}

@app.delete("/report/{report_id}")
async def delete_report(report_id: int, request: Request):
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)

    async with AsyncSessionLocal() as session:
        # Check if user is admin
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")

        result = await session.execute(
            select(Report).where(Report.id == report_id)
        )
        report = result.scalars().first()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        await session.delete(report)
        await session.commit()

    return {"ok": True}

@app.delete("/admin/user/{user_id}")
async def delete_user(user_id: int, request: Request):
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)

    async with AsyncSessionLocal() as session:
        # Sprawdzenie czy admin
        result = await session.execute(select(User).where(User.user_name == user_name))
        current_admin = result.scalars().first()
        if not current_admin or not current_admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")

        # Znajdź użytkownika do usunięcia
        result = await session.execute(select(User).where(User.id == user_id))
        target_user = result.scalars().first()

        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        if target_user.user_name == user_name:
            raise HTTPException(status_code=400, detail="Cannot delete yourself")

        await session.delete(target_user)
        await session.commit()
    return {"ok": True}


# ==================== NEW FEATURES ====================

# --- Helper Functions ---

async def log_activity(session, user_id: int, action: str, details: str = None, ip_address: str = None):
    """Log user activity"""
    log = ActivityLog(user_id=user_id, action=action, details=details, ip_address=ip_address)
    session.add(log)
    await session.commit()

async def get_profanity_words(session):
    """Get list of active profanity words"""
    result = await session.execute(select(ProfanityFilter).where(ProfanityFilter.is_active == True))
    return [r.word for r in result.scalars().all()]

async def check_user_banned(user: User) -> bool:
    """Check if user is banned"""
    if user.is_banned and user.banned_until:
        if datetime.utcnow() < user.banned_until:
            return True
        else:
            user.is_banned = False
            user.banned_until = None
    return False


# --- Enhanced Login with 2FA and Session Management ---

@app.post('/api/login/check')
async def check_login_status(request: Request, username: str = Form(...)):
    """Check if 2FA is required for user"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()
    
    if not user:
        return {"requires_2fa": False, "user_exists": False}
    
    return {"requires_2fa": user.is_2fa_enabled, "user_exists": True}

@app.post('/api/login/verify-2fa')
async def verify_login_2fa(request: Request, username: str = Form(...), totp_code: str = Form(...)):
    """Verify 2FA code during login"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()
        
        if not user or not user.totp_secret:
            raise HTTPException(status_code=400, detail="2FA not configured")
        
        if not verify_totp(user.totp_secret, totp_code):
            raise HTTPException(status_code=400, detail="Invalid 2FA code")
        
        request.session["user_name"] = username
        await log_activity(session, user.id, "LOGIN_2FA", f"User logged in with 2FA", request.client.host if request.client else None)
        
        return {"ok": True, "redirect": "/chat"}

@app.post('/api/login')
async def api_login(request: Request, username: str = Form(...), password: str = Form(...)):
    """API login with session management and rate limiting"""
    async with AsyncSessionLocal() as session:
        # Check rate limiting
        recent_attempts = await session.execute(
            select(LoginAttempt).where(
                LoginAttempt.username == username,
                LoginAttempt.created_at > datetime.utcnow() - timedelta(minutes=5)
            )
        )
        failed_attempts = [a for a in recent_attempts.scalars().all() if not a.success]
        
        if len(failed_attempts) >= 5:
            return {"ok": False, "error": "Zbyt wiele nieudanych prób. Spróbuj za 5 minut."}
        
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()
        
        if not user:
            attempt = LoginAttempt(username=username, ip_address=request.client.host if request.client else None, success=False)
            session.add(attempt)
            await session.commit()
            return {"ok": False, "error": "Błędne dane logowania"}
        
        if not verify_password(password, user.password):
            attempt = LoginAttempt(username=username, ip_address=request.client.host if request.client else None, success=False)
            session.add(attempt)
            await session.commit()
            return {"ok": False, "error": "Błędne dane logowania"}
        
        # Check if banned
        if await check_user_banned(user):
            return {"ok": False, "error": f"Konto zablokowane do {user.banned_until}"}
        
        # Successful login
        attempt = LoginAttempt(username=username, ip_address=request.client.host if request.client else None, success=True)
        session.add(attempt)
        
        # Create session
        session_token = hash_token(generate_reset_token())
        expires_at = datetime.utcnow() + timedelta(days=30)
        
        user_session = UserSession(
            user_id=user.id,
            session_token=session_token,
            device_info=request.headers.get("user-agent", "Unknown")[:255],
            ip_address=request.client.host if request.client else None,
            expires_at=expires_at
        )
        session.add(user_session)
        
        user.last_seen = datetime.utcnow()
        await session.commit()
        
        await log_activity(session, user.id, "LOGIN", f"User logged in", request.client.host if request.client else None)
        
        if user.is_2fa_enabled:
            return {"ok": True, "requires_2fa": True, "username": username}
        
        request.session["user_name"] = username
        return {"ok": True, "redirect": "/chat"}


# --- Password Reset ---

@app.get('/forgot-password')
async def forgot_password_page(request: Request):
    return templates.TemplateResponse('auth.html', {'request': request, 'title': 'Resetowanie hasła'})

@app.post('/api/forgot-password')
async def request_password_reset(request: Request, username: str = Form(...)):
    """Request password reset token"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()
        
        if not user:
            return {"ok": True, "message": "Jeśli użytkownik istnieje, token został wysłany."}
        
        # Generate token
        token = generate_reset_token()
        hashed_token = hash_token(token)
        expires_at = datetime.utcnow() + timedelta(hours=RESET_TOKEN_EXPIRE_HOURS)
        
        reset_token = PasswordResetToken(user_id=user.id, token=hashed_token, expires_at=expires_at)
        session.add(reset_token)
        await session.commit()
        
        # In production, send email here
        # For now, return token for testing
        return {"ok": True, "message": "Token wygenerowany", "debug_token": token}

@app.get('/reset-password')
async def reset_password_page(request: Request, token: str = None):
    if not token:
        return RedirectResponse(url='/forgot-password')
    return templates.TemplateResponse('auth.html', {'request': request, 'title': 'Nowe hasło', 'reset_token': token})

@app.post('/api/reset-password')
async def perform_password_reset(request: Request, token: str = Form(...), new_password: str = Form(...)):
    """Reset password with token"""
    # Check password strength
    is_strong, msg = verify_password_strength(new_password)
    if not is_strong:
        return {"ok": False, "error": msg}
    
    async with AsyncSessionLocal() as session:
        hashed_token = hash_token(token)
        result = await session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token == hashed_token,
                PasswordResetToken.used == False,
                PasswordResetToken.expires_at > datetime.utcnow()
            )
        )
        reset_token = result.scalars().first()
        
        if not reset_token:
            return {"ok": False, "error": "Nieprawidłowy lub wygasły token"}
        
        result = await session.execute(select(User).where(User.id == reset_token.user_id))
        user = result.scalars().first()
        
        if user:
            user.password = hash_password(new_password)
            reset_token.used = True
            await session.commit()
            await log_activity(session, user.id, "PASSWORD_RESET", "Password was reset")
        
        return {"ok": True, "message": "Hasło zostało zresetowane"}


# --- 2FA Management ---

@app.get('/2fa/setup')
async def setup_2fa_page(request: Request):
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.totp_secret:
            user.totp_secret = generate_totp_secret()
            await session.commit()
        
        totp_uri = get_totp_uri(user_name, user.totp_secret)
        
        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(totp_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        qr_code = base64.b64encode(buffered.getvalue()).decode()
        
        return templates.TemplateResponse('2fa_setup.html', {
            'request': request,
            'current_user': user_name,
            'qr_code': qr_code,
            'secret': user.totp_secret
        })

@app.post('/api/2fa/enable')
async def enable_2fa(request: Request, totp_code: str = Form(...)):
    """Enable 2FA after verifying code"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user or not user.totp_secret:
            raise HTTPException(status_code=400, detail="2FA not initialized")
        
        if not verify_totp(user.totp_secret, totp_code):
            return {"ok": False, "error": "Nieprawidłowy kod"}
        
        user.is_2fa_enabled = True
        await session.commit()
        await log_activity(session, user.id, "2FA_ENABLED", "Two-factor authentication enabled")
        
        return {"ok": True}

@app.post('/api/2fa/disable')
async def disable_2fa(request: Request, password: str = Form(...)):
    """Disable 2FA with password confirmation"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user or not verify_password(password, user.password):
            return {"ok": False, "error": "Nieprawidłowe hasło"}
        
        user.is_2fa_enabled = False
        user.totp_secret = None
        await session.commit()
        await log_activity(session, user.id, "2FA_DISABLED", "Two-factor authentication disabled")
        
        return {"ok": True}


# --- Session Management ---

@app.get('/sessions')
async def sessions_page(request: Request):
    """View active sessions"""
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.is_active == True,
                UserSession.expires_at > datetime.utcnow()
            ).order_by(UserSession.last_activity.desc())
        )
        sessions = result.scalars().all()
        
        return templates.TemplateResponse('sessions.html', {
            'request': request,
            'current_user': user_name,
            'sessions': sessions
        })

@app.post('/api/session/revoke/{session_id}')
async def revoke_session(session_id: int, request: Request):
    """Revoke a specific session"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(UserSession).where(
                UserSession.id == session_id,
                UserSession.user_id == user.id
            )
        )
        user_session = result.scalars().first()
        
        if user_session:
            user_session.is_active = False
            await session.commit()
        
        return {"ok": True}

@app.post('/api/sessions/revoke-all')
async def revoke_all_sessions(request: Request):
    """Revoke all sessions except current"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.is_active == True
            )
            .values(is_active=False)
        )
        await session.commit()
        
        return {"ok": True}


# --- Friend System ---

@app.get('/friends')
async def friends_page(request: Request):
    """Friends list page"""
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()

        # Get friends
        result = await session.execute(
            select(Friendship).where(Friendship.user_id == user.id)
        )
        friendships = result.scalars().all()

        friend_ids = [f.friend_id for f in friendships]
        result = await session.execute(select(User).where(User.id.in_(friend_ids)))
        friends = result.scalars().all()

        # Get pending requests
        result = await session.execute(
            select(FriendRequest).where(
                FriendRequest.receiver_id == user.id,
                FriendRequest.status == "pending"
            )
        )
        pending_requests = result.scalars().all()

        return templates.TemplateResponse('friends.html', {
            'request': request,
            'current_user': user_name,
            'friends': friends,
            'pending_requests': pending_requests,
            'active_connections': []  # Will be populated by JS from WebSocket
        })

@app.post('/api/friend/request/{target_username}')
async def send_friend_request(target_username: str, request: Request):
    """Send friend request"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(User).where(User.user_name == target_username))
        target = result.scalars().first()
        
        if not target:
            return {"ok": False, "error": "Użytkownik nie istnieje"}
        
        if target.id == user.id:
            return {"ok": False, "error": "Nie możesz dodać samego siebie"}
        
        # Check if already friends
        result = await session.execute(
            select(Friendship).where(
                or_(
                    and_(Friendship.user_id == user.id, Friendship.friend_id == target.id),
                    and_(Friendship.user_id == target.id, Friendship.friend_id == user.id)
                )
            )
        )
        if result.scalars().first():
            return {"ok": False, "error": "Już jesteście znajomymi"}
        
        # Check if request already exists
        result = await session.execute(
            select(FriendRequest).where(
                FriendRequest.sender_id == user.id,
                FriendRequest.receiver_id == target.id,
                FriendRequest.status == "pending"
            )
        )
        if result.scalars().first():
            return {"ok": False, "error": "Zaproszenie już wysłane"}
        
        friend_request = FriendRequest(sender_id=user.id, receiver_id=target.id)
        session.add(friend_request)
        await session.commit()
        
        return {"ok": True}

@app.post('/api/friend/accept/{request_id}')
async def accept_friend_request(request_id: int, request: Request):
    """Accept friend request"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(FriendRequest).where(
                FriendRequest.id == request_id,
                FriendRequest.receiver_id == user.id
            )
        )
        friend_request = result.scalars().first()
        
        if not friend_request:
            return {"ok": False, "error": "Zaproszenie nie istnieje"}
        
        friend_request.status = "accepted"
        
        friendship = Friendship(user_id=friend_request.sender_id, friend_id=user.id)
        session.add(friendship)
        await session.commit()
        
        return {"ok": True}

@app.post('/api/friend/reject/{request_id}')
async def reject_friend_request(request_id: int, request: Request):
    """Reject friend request"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(FriendRequest).where(
                FriendRequest.id == request_id
            )
        )
        friend_request = result.scalars().first()
        
        if friend_request:
            friend_request.status = "rejected"
            await session.commit()
        
        return {"ok": True}

@app.delete('/api/friend/{friend_id}')
async def remove_friend(friend_id: int, request: Request):
    """Remove friend"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(
            delete(Friendship).where(
                or_(
                    and_(Friendship.user_id == user.id, Friendship.friend_id == friend_id),
                    and_(Friendship.user_id == friend_id, Friendship.friend_id == user.id)
                )
            )
        )
        await session.commit()
        
        return {"ok": True}


# --- Message Reactions ---

@app.post('/api/message/{message_id}/react')
async def add_reaction(message_id: int, request: Request, emoji: str = Form(...)):
    """Add reaction to message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        message = result.scalars().first()
        
        if not message:
            return {"ok": False, "error": "Wiadomość nie istnieje"}
        
        # Check if reaction already exists
        result = await session.execute(
            select(MessageReaction).where(
                MessageReaction.message_id == message_id,
                MessageReaction.user_name == user_name,
                MessageReaction.emoji == emoji
            )
        )
        existing = result.scalars().first()
        
        if existing:
            await session.delete(existing)
        else:
            reaction = MessageReaction(message_id=message_id, user_name=user_name, emoji=emoji)
            session.add(reaction)
        
        await session.commit()
        
        # Notify via websocket if needed
        return {"ok": True}

@app.get('/api/message/{message_id}/reactions')
async def get_reactions(message_id: int):
    """Get all reactions for a message"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MessageReaction).where(MessageReaction.message_id == message_id)
        )
        reactions = result.scalars().all()
        
        return [{"emoji": r.emoji, "user": r.user_name} for r in reactions]


# --- Message Edit & Reply ---

@app.put('/api/message/{message_id}')
async def edit_message(message_id: int, request: Request, text: str = Form(...)):
    """Edit own message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        message = result.scalars().first()
        
        if not message or message.sender_name != user_name:
            raise HTTPException(status_code=403, detail="Not your message")
        
        # Apply profanity filter
        profanity_words = await get_profanity_words(session)
        message.text = censor_profanity(text, profanity_words)
        message.edited_at = datetime.utcnow()
        
        await session.commit()
        
        return {"ok": True}

@app.post('/api/message/{message_id}/reply')
async def reply_to_message(message_id: int, request: Request, text: str = Form(...), to: str = Form(...)):
    """Reply to a message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        original = result.scalars().first()
        
        if not original:
            return {"ok": False, "error": "Wiadomość nie istnieje"}
        
        # Apply profanity filter
        profanity_words = await get_profanity_words(session)
        filtered_text = censor_profanity(text, profanity_words)
        
        new_msg = Message(
            sender_name=user_name,
            receiver_name=to,
            text=filtered_text,
            reply_to_id=message_id
        )
        session.add(new_msg)
        await session.commit()
        
        # Notify via websocket
        payload = {
            "type": "message",
            "sender": user_name,
            "to": to,
            "text": filtered_text,
            "id": new_msg.id,
            "reply_to_id": message_id
        }
        await manager.send_personal_message(payload, to)
        await manager.send_personal_message(payload, user_name)
        
        return {"ok": True}


# --- Group Chats ---

@app.get('/groups')
async def groups_page(request: Request):
    """Groups list page"""
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(GroupMember).where(GroupMember.user_id == user.id, GroupMember.is_banned == False)
        )
        memberships = result.scalars().all()
        
        group_ids = [m.group_id for m in memberships]
        result = await session.execute(select(Group).where(Group.id.in_(group_ids), Group.is_active == True))
        groups = result.scalars().all()
        
        return templates.TemplateResponse('groups.html', {
            'request': request,
            'current_user': user_name,
            'groups': groups
        })

@app.post('/api/groups/create')
async def create_group(request: Request, name: str = Form(...), description: str = Form(None)):
    """Create a new group"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        group = Group(name=name, description=description, owner_id=user.id)
        session.add(group)
        await session.flush()
        
        # Add owner as member
        member = GroupMember(group_id=group.id, user_id=user.id, role="owner")
        session.add(member)
        await session.commit()
        
        return {"ok": True, "group_id": group.id}

@app.get('/api/groups/{group_id}/messages')
async def get_group_messages(group_id: int, request: Request):
    """Get messages from group chat"""
    user_name = request.session.get("user_name")
    if not user_name:
        return []
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GroupMessage).where(
                GroupMessage.group_id == group_id,
                GroupMessage.is_deleted == False
            ).order_by(GroupMessage.created_at.asc())
        )
        messages = result.scalars().all()
        
        return [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "text": m.text,
                "file": m.file_name,
                "file_url": m.file_path,
                "created_at": m.created_at.isoformat(),
                "reply_to_id": m.reply_to_id
            }
            for m in messages
        ]

@app.post('/api/groups/{group_id}/message')
async def send_group_message(group_id: int, request: Request, text: str = Form(...)):
    """Send message to group chat"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        # Check membership
        result = await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == user.id,
                GroupMember.is_banned == False
            )
        )
        member = result.scalars().first()
        
        if not member:
            raise HTTPException(status_code=403, detail="Not a group member")
        
        # Apply profanity filter
        profanity_words = await get_profanity_words(session)
        filtered_text = censor_profanity(text, profanity_words)
        
        msg = GroupMessage(group_id=group_id, sender_id=user.id, text=filtered_text)
        session.add(msg)
        await session.commit()
        
        # Notify group members via websocket
        payload = {
            "type": "group_message",
            "group_id": group_id,
            "sender": user_name,
            "text": filtered_text,
            "id": msg.id
        }
        
        for conn_username in manager.active_connections.keys():
            await manager.send_personal_message(payload, conn_username)
        
        return {"ok": True}

@app.post('/api/groups/{group_id}/invite')
async def invite_to_group(group_id: int, request: Request, username: str = Form(...)):
    """Invite user to group"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(User).where(User.user_name == username))
        target = result.scalars().first()
        
        if not target:
            return {"ok": False, "error": "Użytkownik nie istnieje"}
        
        # Check if already member
        result = await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.user_id == target.id
            )
        )
        if result.scalars().first():
            return {"ok": False, "error": "Użytkownik już jest w grupie"}
        
        member = GroupMember(group_id=group_id, user_id=target.id, role="member")
        session.add(member)
        await session.commit()
        
        return {"ok": True}


# --- Admin Features ---

@app.get('/admin/activity-logs')
async def activity_logs_page(request: Request, user_id: int = None):
    """View activity logs"""
    user_name = request.session.get("user_name")
    if not user_name:
        return RedirectResponse(url='/login')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        query = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(100)
        if user_id:
            query = query.where(ActivityLog.user_id == user_id)
        
        result = await session.execute(query)
        logs = result.scalars().all()
        
        return templates.TemplateResponse('activity_logs.html', {
            'request': request,
            'current_user': user_name,
            'logs': logs
        })

@app.post('/api/admin/warn/{user_id}')
async def warn_user(user_id: int, request: Request, reason: str = Form(...)):
    """Issue warning to user"""
    admin_name = request.session.get("user_name")
    if not admin_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == admin_name))
        admin = result.scalars().first()
        
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(User).where(User.id == user_id))
        target = result.scalars().first()
        
        if not target:
            return {"ok": False, "error": "Użytkownik nie istnieje"}
        
        warning = UserWarning(user_id=user_id, admin_id=admin.id, reason=reason)
        session.add(warning)
        await session.commit()
        
        await log_activity(session, admin.id, "WARNING_ISSUED", f"Warning issued to {target.user_name}: {reason}")
        
        return {"ok": True}

@app.post('/api/admin/ban/{user_id}')
async def ban_user(user_id: int, request: Request, duration_hours: int = Form(24), reason: str = Form(...)):
    """Temporarily ban user"""
    admin_name = request.session.get("user_name")
    if not admin_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == admin_name))
        admin = result.scalars().first()
        
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(User).where(User.id == user_id))
        target = result.scalars().first()
        
        if not target:
            return {"ok": False, "error": "Użytkownik nie istnieje"}
        
        if target.is_admin:
            return {"ok": False, "error": "Cannot ban admin"}
        
        target.is_banned = True
        target.banned_until = datetime.utcnow() + timedelta(hours=duration_hours)
        await session.commit()
        
        await log_activity(session, admin.id, "USER_BANNED", f"User {target.user_name} banned for {duration_hours}h: {reason}")
        
        return {"ok": True}

@app.post('/api/admin/unban/{user_id}')
async def unban_user(user_id: int, request: Request):
    """Unban user"""
    admin_name = request.session.get("user_name")
    if not admin_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == admin_name))
        admin = result.scalars().first()
        
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(User).where(User.id == user_id))
        target = result.scalars().first()
        
        if target:
            target.is_banned = False
            target.banned_until = None
            await session.commit()
        
        return {"ok": True}

@app.post('/api/admin/profanity/add')
async def add_profanity_word(request: Request, word: str = Form(...), replacement: str = Form("****")):
    """Add word to profanity filter"""
    admin_name = request.session.get("user_name")
    if not admin_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == admin_name))
        admin = result.scalars().first()
        
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        try:
            profanity = ProfanityFilter(word=word, replacement=replacement)
            session.add(profanity)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return {"ok": False, "error": "Słowo już istnieje"}
        
        return {"ok": True}

@app.delete('/api/admin/profanity/{word_id}')
async def remove_profanity_word(word_id: int, request: Request):
    """Remove word from profanity filter"""
    admin_name = request.session.get("user_name")
    if not admin_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == admin_name))
        admin = result.scalars().first()
        
        if not admin or not admin.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(ProfanityFilter).where(ProfanityFilter.id == word_id))
        profanity = result.scalars().first()
        
        if profanity:
            await session.delete(profanity)
            await session.commit()
        
        return {"ok": True}


# --- User Profile & Settings ---

@app.get('/profile/{username}')
async def profile_page(username: str, request: Request):
    """View user profile"""
    current_user = request.session.get("user_name")
    if not current_user:
        return RedirectResponse(url='/login')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Check if friends
        is_friend = False
        if current_user:
            result_current = await session.execute(select(User).where(User.user_name == current_user))
            current = result_current.scalars().first()
            result = await session.execute(
                select(Friendship).where(
                    or_(
                        and_(Friendship.user_id == current.id, Friendship.friend_id == user.id),
                        and_(Friendship.user_id == user.id, Friendship.friend_id == current.id)
                    )
                )
            )
            is_friend = result.scalars().first() is not None
        
        return templates.TemplateResponse('profile.html', {
            'request': request,
            'current_user': current_user,
            'profile_user': user,
            'is_friend': is_friend
        })

@app.post('/api/profile/dark-mode')
async def toggle_dark_mode(request: Request, enabled: bool = Form(...)):
    """Toggle dark mode preference"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if user:
            user.dark_mode = enabled
            await session.commit()
        
        return {"ok": True}

@app.post('/api/profile/status')
async def update_status(request: Request, status: str = Form(...)):
    """Update user status"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if user:
            user.status = status
            await session.commit()
        
        await manager.broadcast_status(user_name, status)
        return {"ok": True}


# --- Chat Export ---

@app.get('/api/chat/export/{other_user}')
async def export_chat(other_user: str, request: Request, format: str = "json"):
    """Export chat history"""
    current_user = request.session.get("user_name")
    if not current_user:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        query = select(Message).where(
            or_(
                and_(Message.sender_name == current_user, Message.receiver_name == other_user),
                and_(Message.sender_name == other_user, Message.receiver_name == current_user)
            )
        ).order_by(Message.created_at.asc())
        
        result = await session.execute(query)
        messages = result.scalars().all()
        
        if format == "json":
            data = [
                {
                    "id": m.id,
                    "sender": m.sender_name,
                    "text": m.text,
                    "file": m.file_name,
                    "created_at": m.created_at.isoformat()
                }
                for m in messages
            ]
            return JSONResponse(content=data)
        
        elif format == "txt":
            text = "\n".join([f"[{m.created_at}] {m.sender_name}: {m.text or '[file]'}" for m in messages])
            return StreamingResponse(io.StringIO(text), media_type="text/plain", headers={"Content-Disposition": f"attachment; filename=chat_{other_user}.txt"})
        
        return {"error": "Invalid format"}


# --- Push Notifications ---

@app.post('/api/push/register')
async def register_push_token(request: Request, token: str = Form(...), device_type: str = Form("web")):
    """Register push notification token"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        push_token = PushNotificationToken(user_id=user.id, token=token, device_type=device_type)
        session.add(push_token)
        await session.commit()
        
        return {"ok": True}


# --- Search ---

@app.get('/api/search/users')
async def search_users(q: str, request: Request):
    """Search users"""
    user_name = request.session.get("user_name")
    if not user_name:
        return []
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(
                User.user_name.like(f"%{q}%"),
                User.user_name != user_name
            ).limit(20)
        )
        users = result.scalars().all()
        
        return [{"username": u.user_name, "avatar": u.avatar_url, "status": u.status} for u in users]

@app.get('/api/search/messages')
async def search_messages(q: str, request: Request):
    """Search messages"""
    user_name = request.session.get("user_name")
    if not user_name:
        return []

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Message).where(
                or_(
                    Message.sender_name == user_name,
                    Message.receiver_name == user_name
                ),
                Message.text.like(f"%{q}%"),
                Message.is_deleted == False
            ).order_by(Message.created_at.desc()).limit(50)
        )
        messages = result.scalars().all()

        return [
            {
                "id": m.id,
                "sender": m.sender_name,
                "receiver": m.receiver_name,
                "text": m.text,
                "created_at": m.created_at.isoformat()
            }
            for m in messages
        ]


# ==================== FAZA 1: BEZPIECZEŃSTWO ====================

@app.get('/api/security/2fa/backup-codes')
async def generate_backup_codes(request: Request):
    """Generate 2FA backup codes"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user:
            raise HTTPException(status_code=404)
        
        # Generate 10 backup codes
        import secrets
        backup_codes = [secrets.token_hex(4).upper() for _ in range(10)]
        
        # Store hashed codes
        for code in backup_codes:
            backup_code = TwoFABackupCode(user_id=user.id, code_hash=hash_password(code))
            session.add(backup_code)
        
        await session.commit()
        
        return {"ok": True, "codes": backup_codes}

@app.post('/api/security/2fa/verify-backup')
async def verify_backup_code(request: Request, code: str = Form(...)):
    """Verify 2FA backup code"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(TwoFABackupCode).where(
                TwoFABackupCode.user_id == user.id,
                TwoFABackupCode.used == False
            )
        )
        backup_codes = result.scalars().all()
        
        for bc in backup_codes:
            if verify_password(code, bc.code_hash):
                bc.used = True
                await session.commit()
                return {"ok": True}
        
        return {"ok": False, "error": "Nieprawidłowy kod zapasowy"}

@app.get('/api/security/login-history')
async def get_login_history(request: Request):
    """Get user login history"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(LoginHistory).where(LoginHistory.user_id == user.id)
            .order_by(LoginHistory.created_at.desc()).limit(50)
        )
        history = result.scalars().all()
        
        return [{
            "id": h.id,
            "ip": h.ip_address,
            "device": h.device_info,
            "location": h.location,
            "success": h.success,
            "date": h.created_at.isoformat()
        } for h in history]

@app.post('/api/security/block/{blocked_username}')
async def block_user(blocked_username: str, request: Request):
    """Block a user"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(User).where(User.user_name == blocked_username))
        blocked = result.scalars().first()
        
        if not blocked:
            return {"ok": False, "error": "Użytkownik nie istnieje"}
        
        if blocked.id == user.id:
            return {"ok": False, "error": "Nie możesz zablokować samego siebie"}
        
        try:
            block = BlockedUser(user_id=user.id, blocked_user_id=blocked.id)
            session.add(block)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            return {"ok": False, "error": "Użytkownik już zablokowany"}

@app.delete('/api/security/unblock/{blocked_username}')
async def unblock_user(blocked_username: str, request: Request):
    """Unblock a user"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(User).where(User.user_name == blocked_username))
        blocked = result.scalars().first()
        
        if blocked:
            await session.execute(
                delete(BlockedUser).where(
                    BlockedUser.user_id == user.id,
                    BlockedUser.blocked_user_id == blocked.id
                )
            )
            await session.commit()
        
        return {"ok": True}

@app.get('/api/security/blocked-users')
async def get_blocked_users(request: Request):
    """Get list of blocked users"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(BlockedUser).where(BlockedUser.user_id == user.id)
        )
        blocked = result.scalars().all()
        
        blocked_ids = [b.blocked_user_id for b in blocked]
        result = await session.execute(select(User).where(User.id.in_(blocked_ids)))
        users = result.scalars().all()
        
        return [{"id": u.id, "username": u.user_name, "avatar": u.avatar_url} for u in users]


# ==================== FAZA 2: WIADOMOŚCI ====================

@app.post('/api/message/{message_id}/pin')
async def pin_message(message_id: int, request: Request):
    """Pin a message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(Message).where(Message.id == message_id))
        message = result.scalars().first()
        
        if not message:
            return {"ok": False, "error": "Wiadomość nie istnieje"}
        
        pin = PinnedMessage(message_id=message_id, user_id=user.id, pinned_by=user_name)
        session.add(pin)
        await session.commit()
        
        return {"ok": True}

@app.delete('/api/message/{message_id}/unpin')
async def unpin_message(message_id: int, request: Request):
    """Unpin a message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(PinnedMessage).where(PinnedMessage.message_id == message_id)
        )
        await session.commit()
        
        return {"ok": True}

@app.get('/api/messages/pinned/{chat_user}')
async def get_pinned_messages(chat_user: str, request: Request):
    """Get pinned messages for a chat"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(Message).where(
                or_(
                    and_(Message.sender_name == user_name, Message.receiver_name == chat_user),
                    and_(Message.sender_name == chat_user, Message.receiver_name == user_name)
                )
            )
        )
        chat_messages = result.scalars().all()
        chat_msg_ids = [m.id for m in chat_messages]
        
        result = await session.execute(
            select(PinnedMessage).where(
                PinnedMessage.message_id.in_(chat_msg_ids)
            )
        )
        pinned = result.scalars().all()
        
        pinned_msg_ids = [p.message_id for p in pinned]
        result = await session.execute(
            select(Message).where(Message.id.in_(pinned_msg_ids))
            .options(selectinload(Message.reply_to))
        )
        messages = result.scalars().all()
        
        return [{
            "id": m.id,
            "text": m.text,
            "sender": m.sender_name,
            "date": m.created_at.isoformat()
        } for m in messages]

@app.post('/api/message/{message_id}/star')
async def star_message(message_id: int, request: Request):
    """Star/favorite a message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        try:
            starred = StarredMessage(message_id=message_id, user_id=user.id)
            session.add(starred)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            return {"ok": False, "error": "Już oznaczone"}

@app.delete('/api/message/{message_id}/unstar')
async def unstar_message(message_id: int, request: Request):
    """Unstar a message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(
            delete(StarredMessage).where(
                StarredMessage.message_id == message_id,
                StarredMessage.user_id == user.id
            )
        )
        await session.commit()
        
        return {"ok": True}

@app.get('/api/messages/starred')
async def get_starred_messages(request: Request):
    """Get all starred messages"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(StarredMessage).where(StarredMessage.user_id == user.id)
            .order_by(StarredMessage.created_at.desc())
        )
        starred = result.scalars().all()
        
        msg_ids = [s.message_id for s in starred]
        result = await session.execute(
            select(Message).where(Message.id.in_(msg_ids))
            .options(selectinload(Message.reply_to))
        )
        messages = result.scalars().all()
        
        return [{
            "id": m.id,
            "text": m.text,
            "sender": m.sender_name,
            "date": m.created_at.isoformat()
        } for m in messages]

@app.post('/api/message/{message_id}/undo')
async def undo_send_message(message_id: int, request: Request):
    """Undo send message (within 5 minutes)"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        message = result.scalars().first()
        
        if not message or message.sender_name != user_name:
            raise HTTPException(status_code=403)
        
        # Check if within 5 minutes
        if datetime.utcnow() - message.created_at > timedelta(minutes=5):
            return {"ok": False, "error": "Zbyt późno na cofnięcie"}
        
        message.is_deleted = True
        message.text = "Wiadomość została cofnięta"
        await session.commit()
        
        return {"ok": True}


# ==================== FAZA 3: GŁOSOWE, WIDEO, ANKIETY ====================

@app.post('/api/message/voice')
async def send_voice_message(
    request: Request,
    to: str = Form(...),
    duration: int = Form(...),
    waveform: str = Form(None)
):
    """Send voice message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        # Create message
        msg = Message(
            sender_name=user_name,
            receiver_name=to,
            text=f"[Wiadomość głosowa {duration}s]",
            file_type="voice"
        )
        session.add(msg)
        await session.flush()
        
        # Create voice metadata
        voice = VoiceMessage(message_id=msg.id, duration=duration, waveform=waveform)
        session.add(voice)
        
        # Update stats
        await update_user_stats(session, user_name, "voice_messages_sent")
        
        await session.commit()
        
        payload = {
            "type": "voice_message",
            "sender": user_name,
            "to": to,
            "duration": duration,
            "id": msg.id
        }
        await manager.send_personal_message(payload, to)
        await manager.send_personal_message(payload, user_name)
        
        return {"ok": True, "id": msg.id}

@app.post('/api/message/video')
async def send_video_message(
    request: Request,
    to: str = Form(...),
    file: UploadFile = File(...),
    duration: int = Form(None),
    width: int = Form(None),
    height: int = Form(None)
):
    """Send video message"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    file_ext = file.filename.split('.')[-1]
    filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = f"{UPLOAD_DIR}/{filename}"
    
    with open(file_path, "wb") as f:
        f.write(await file.read())
    
    async with AsyncSessionLocal() as session:
        msg = Message(
            sender_name=user_name,
            receiver_name=to,
            text="[Wiadomość wideo]",
            file_path=f"/uploads/{filename}",
            file_name=filename,
            file_type="video"
        )
        session.add(msg)
        await session.flush()
        
        video = VideoMessage(
            message_id=msg.id,
            duration=duration,
            width=width,
            height=height
        )
        session.add(video)
        
        await session.commit()
        
        payload = {
            "type": "video_message",
            "sender": user_name,
            "to": to,
            "file_url": f"/uploads/{filename}",
            "duration": duration,
            "id": msg.id
        }
        await manager.send_personal_message(payload, to)
        await manager.send_personal_message(payload, user_name)
        
        return {"ok": True, "id": msg.id}

@app.post('/api/poll/create')
async def create_poll(
    request: Request,
    to: str = Form(...),
    question: str = Form(...),
    options: str = Form(...),  # JSON array
    multiple_choice: bool = Form(False)
):
    """Create a poll"""
    import json
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    option_list = json.loads(options)
    
    async with AsyncSessionLocal() as session:
        msg = Message(
            sender_name=user_name,
            receiver_name=to,
            text=f"[Ankieta: {question}]"
        )
        session.add(msg)
        await session.flush()
        
        poll = Poll(
            message_id=msg.id,
            question=question,
            multiple_choice=multiple_choice
        )
        session.add(poll)
        await session.flush()
        
        for opt_text in option_list:
            option = PollOption(poll_id=poll.id, text=opt_text)
            session.add(option)
        
        await update_user_stats(session, user_name, "polls_created")
        await session.commit()
        
        return {"ok": True, "poll_id": poll.id, "message_id": msg.id}

@app.post('/api/poll/{poll_id}/vote')
async def vote_poll(poll_id: int, request: Request, option_id: int = Form(...)):
    """Vote in a poll"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        # Check if already voted
        result = await session.execute(
            select(PollVote).where(
                PollVote.poll_id == poll_id,
                PollVote.voter_name == user_name
            )
        )
        if result.scalars().first():
            return {"ok": False, "error": "Już głosowałeś"}
        
        vote = PollVote(poll_id=poll_id, option_id=option_id, voter_name=user_name)
        session.add(vote)
        
        # Update vote count
        await session.execute(
            update(PollOption).where(PollOption.id == option_id).values(
                vote_count=PollOption.vote_count + 1
            )
        )
        
        await session.commit()
        return {"ok": True}

@app.get('/api/poll/{poll_id}')
async def get_poll(poll_id: int):
    """Get poll details"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Poll).where(Poll.id == poll_id)
            .options(selectinload(Poll.options), selectinload(Poll.votes))
        )
        poll = result.scalars().first()
        
        if not poll:
            raise HTTPException(status_code=404)
        
        return {
            "id": poll.id,
            "question": poll.question,
            "multiple_choice": poll.multiple_choice,
            "options": [{"id": o.id, "text": o.text, "votes": o.vote_count} for o in poll.options],
            "total_votes": sum(o.vote_count for o in poll.options)
        }


# ==================== FAZA 4: KONTAKTY ====================

@app.get('/api/contacts/suggestions')
async def get_contact_suggestions(request: Request):
    """Get suggested friends"""
    user_name = request.session.get("user_name")
    if not user_name:
        return []
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        # Get current friends
        result = await session.execute(
            select(Friendship).where(
                or_(
                    Friendship.user_id == user.id,
                    Friendship.friend_id == user.id
                )
            )
        )
        friendships = result.scalars().all()
        friend_ids = set()
        for f in friendships:
            friend_ids.add(f.user_id if f.friend_id == user.id else f.friend_id)
        friend_ids.add(user.id)
        
        # Get users not in friends, sorted by common friends
        result = await session.execute(
            select(User).where(~User.id.in_(friend_ids)).limit(20)
        )
        suggestions = result.scalars().all()
        
        return [{"id": u.id, "username": u.user_name, "avatar": u.avatar_url} for u in suggestions]

@app.get('/api/contacts/blocked')
async def get_blocked_list(request: Request):
    """Get blocked contacts"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(BlockedUser).where(BlockedUser.user_id == user.id)
        )
        blocked = result.scalars().all()
        
        blocked_ids = [b.blocked_user_id for b in blocked]
        result = await session.execute(select(User).where(User.id.in_(blocked_ids)))
        users = result.scalars().all()
        
        return [{"id": u.id, "username": u.user_name} for u in users]


# ==================== FAZA 5: WYGLĄD ====================

@app.get('/api/themes')
async def get_themes():
    """Get available themes"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ChatTheme))
        themes = result.scalars().all()
        
        return [{
            "id": t.id,
            "name": t.name,
            "primary": t.primary_color,
            "secondary": t.secondary_color,
            "is_dark": t.is_dark,
            "is_premium": t.is_premium
        } for t in themes]

@app.post('/api/theme/select')
async def select_theme(request: Request, theme_id: int = Form(...)):
    """Select user theme"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(UserTheme).where(UserTheme.user_id == user.id))
        user_theme = result.scalars().first()
        
        if user_theme:
            user_theme.theme_id = theme_id
        else:
            user_theme = UserTheme(user_id=user.id, theme_id=theme_id)
            session.add(user_theme)
        
        await session.commit()
        return {"ok": True}

@app.post('/api/wallpaper/set')
async def set_wallpaper(
    request: Request,
    wallpaper_url: str = Form(...),
    chat_with: str = Form(None),
    wallpaper_type: str = Form("image")
):
    """Set chat wallpaper"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(ChatWallpaper).where(
                ChatWallpaper.user_id == user.id,
                ChatWallpaper.chat_with == chat_with
            )
        )
        wallpaper = result.scalars().first()
        
        if wallpaper:
            wallpaper.wallpaper_url = wallpaper_url
            wallpaper.wallpaper_type = wallpaper_type
        else:
            wallpaper = ChatWallpaper(
                user_id=user.id,
                chat_with=chat_with,
                wallpaper_url=wallpaper_url,
                wallpaper_type=wallpaper_type
            )
            session.add(wallpaper)
        
        await session.commit()
        return {"ok": True}


# ==================== FAZA 6: POWIADOMIENIA ====================

@app.post('/api/chat/mute')
async def mute_chat(
    request: Request,
    chat_with: str = Form(...),
    duration_hours: int = Form(None)  # NULL = permanent
):
    """Mute a chat"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        muted_until = None
        if duration_hours:
            muted_until = datetime.utcnow() + timedelta(hours=duration_hours)
        
        try:
            mute = MutedChat(user_id=user.id, chat_with=chat_with, muted_until=muted_until)
            session.add(mute)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            # Update existing
            result = await session.execute(
                select(MutedChat).where(
                    MutedChat.user_id == user.id,
                    MutedChat.chat_with == chat_with
                )
            )
            mute = result.scalars().first()
            if mute:
                mute.muted_until = muted_until
                await session.commit()
            return {"ok": True}

@app.delete('/api/chat/unmute/{chat_with}')
async def unmute_chat(chat_with: str, request: Request):
    """Unmute a chat"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(
            delete(MutedChat).where(
                MutedChat.user_id == user.id,
                MutedChat.chat_with == chat_with
            )
        )
        await session.commit()
        
        return {"ok": True}

@app.get('/api/chat/muted')
async def get_muted_chats(request: Request):
    """Get muted chats"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(MutedChat).where(MutedChat.user_id == user.id)
        )
        muted = result.scalars().all()
        
        return [{
            "chat_with": m.chat_with,
            "muted_until": m.muted_until.isoformat() if m.muted_until else "permanent"
        } for m in muted]


# ==================== FAZA 7: PLIKI ====================

@app.post('/api/files/upload')
async def upload_to_storage(
    request: Request,
    file: UploadFile = File(...)
):
    """Upload file to cloud storage"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    file_ext = file.filename.split('.')[-1]
    filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = f"{UPLOAD_DIR}/storage/{filename}"
    
    os.makedirs(f"{UPLOAD_DIR}/storage", exist_ok=True)
    
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        storage = FileStorage(
            user_id=user.id,
            file_path=file_path,
            file_name=file.filename,
            file_size=len(content),
            file_type=file.content_type
        )
        session.add(storage)
        await session.commit()
        
        return {
            "ok": True,
            "file_id": storage.id,
            "url": f"/uploads/storage/{filename}",
            "size": len(content)
        }

@app.get('/api/files/storage')
async def get_storage_files(request: Request):
    """Get user's cloud storage files"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(FileStorage).where(FileStorage.user_id == user.id)
            .order_by(FileStorage.created_at.desc())
        )
        files = result.scalars().all()
        
        total_size = sum(f.file_size for f in files)
        
        return {
            "files": [{
                "id": f.id,
                "name": f.file_name,
                "size": f.file_size,
                "type": f.file_type,
                "url": f"/{f.file_path}",
                "date": f.created_at.isoformat()
            } for f in files],
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2)
        }

@app.delete('/api/files/storage/{file_id}')
async def delete_storage_file(file_id: int, request: Request):
    """Delete file from storage"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(FileStorage).where(
                FileStorage.id == file_id,
                FileStorage.user_id == user.id
            )
        )
        storage = result.scalars().first()
        
        if storage:
            if os.path.exists(storage.file_path):
                os.remove(storage.file_path)
            await session.delete(storage)
            await session.commit()
        
        return {"ok": True}

@app.post('/api/qr/generate')
async def generate_qr(request: Request, data: str = Form(...)):
    """Generate QR code"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    filename = f"qr_{uuid.uuid4()}.png"
    file_path = f"{UPLOAD_DIR}/qr/{filename}"
    os.makedirs(f"{UPLOAD_DIR}/qr", exist_ok=True)
    img.save(file_path)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        qr_data = QRCodeData(
            user_id=user.id,
            data=data,
            qr_image_path=file_path
        )
        session.add(qr_data)
        await session.commit()
        
        return {
            "ok": True,
            "qr_url": f"/uploads/qr/{filename}",
            "id": qr_data.id
        }


# ==================== FAZA 8: USTAWIENIA ====================

@app.get('/api/settings/export')
async def export_user_data(request: Request):
    """Export all user data (GDPR)"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        # Get all messages
        result = await session.execute(
            select(Message).where(
                or_(
                    Message.sender_name == user_name,
                    Message.receiver_name == user_name
                )
            )
        )
        messages = result.scalars().all()
        
        # Get friends
        result = await session.execute(
            select(Friendship).where(Friendship.user_id == user.id)
        )
        friendships = result.scalars().all()
        
        data = {
            "user": {
                "username": user.user_name,
                "email": user.email,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "status": user.status
            },
            "messages": [{
                "id": m.id,
                "sender": m.sender_name,
                "receiver": m.receiver_name,
                "text": m.text,
                "date": m.created_at.isoformat()
            } for m in messages],
            "friends_count": len(friendships),
            "export_date": datetime.utcnow().isoformat()
        }
        
        return data

@app.post('/api/settings/auto-delete')
async def set_auto_delete(
    request: Request,
    hours: int = Form(...),
    chat_with: str = Form(None),
    enabled: bool = Form(True)
):
    """Set auto-delete setting"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(AutoDeleteSetting).where(
                AutoDeleteSetting.user_id == user.id,
                AutoDeleteSetting.chat_with == chat_with
            )
        )
        setting = result.scalars().first()
        
        if setting:
            setting.delete_after_hours = hours
            setting.enabled = enabled
        else:
            setting = AutoDeleteSetting(
                user_id=user.id,
                chat_with=chat_with,
                delete_after_hours=hours,
                enabled=enabled
            )
            session.add(setting)
        
        await session.commit()
        return {"ok": True}

@app.post('/api/settings/language')
async def set_language(request: Request, language: str = Form(...)):
    """Set user language"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(UserLanguage).where(UserLanguage.user_id == user.id)
        )
        lang = result.scalars().first()
        
        if lang:
            lang.language = language
        else:
            lang = UserLanguage(user_id=user.id, language=language)
            session.add(lang)
        
        await session.commit()
        return {"ok": True}


# ==================== FAZA 9: INNE ====================

@app.get('/api/stats')
async def get_user_stats(request: Request):
    """Get user statistics"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(UserStatistic).where(UserStatistic.user_id == user.id)
        )
        stats = result.scalars().first()
        
        if not stats:
            # Create default stats
            stats = UserStatistic(user_id=user.id)
            session.add(stats)
            await session.commit()
        
        return {
            "messages_sent": stats.messages_sent,
            "messages_received": stats.messages_received,
            "files_sent": stats.files_sent,
            "voice_messages": stats.voice_messages_sent,
            "stickers_sent": stats.stickers_sent,
            "polls_created": stats.polls_created
        }

@app.post('/api/focus-mode')
async def toggle_focus_mode(
    request: Request,
    enabled: bool = Form(...),
    hide_sidebar: bool = Form(False),
    hide_notifications: bool = Form(False)
):
    """Toggle focus mode"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(FocusMode).where(FocusMode.user_id == user.id)
        )
        focus = result.scalars().first()
        
        if focus:
            focus.enabled = enabled
            focus.hide_sidebar = hide_sidebar
            focus.hide_notifications = hide_notifications
        else:
            focus = FocusMode(
                user_id=user.id,
                enabled=enabled,
                hide_sidebar=hide_sidebar,
                hide_notifications=hide_notifications
            )
            session.add(focus)
        
        await session.commit()
        return {"ok": True}

@app.get('/api/focus-mode')
async def get_focus_mode(request: Request):
    """Get focus mode settings"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(FocusMode).where(FocusMode.user_id == user.id)
        )
        focus = result.scalars().first()
        
        if not focus:
            return {"enabled": False, "hide_sidebar": False, "hide_notifications": False}
        
        return {
            "enabled": focus.enabled,
            "hide_sidebar": focus.hide_sidebar,
            "hide_notifications": focus.hide_notifications,
            "quiet_hours": {"start": focus.quiet_hours_start, "end": focus.quiet_hours_end}
        }

@app.post('/api/shortcuts/set')
async def set_keyboard_shortcut(
    request: Request,
    action: str = Form(...),
    shortcut: str = Form(...)
):
    """Set custom keyboard shortcut"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        try:
            shortcut_obj = KeyboardShortcut(user_id=user.id, action=action, shortcut=shortcut)
            session.add(shortcut_obj)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            result = await session.execute(
                select(KeyboardShortcut).where(
                    KeyboardShortcut.user_id == user.id,
                    KeyboardShortcut.action == action
                )
            )
            sc = result.scalars().first()
            if sc:
                sc.shortcut = shortcut
                await session.commit()
            return {"ok": True}

@app.get('/api/shortcuts')
async def get_keyboard_shortcuts(request: Request):
    """Get user's keyboard shortcuts"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(KeyboardShortcut).where(KeyboardShortcut.user_id == user.id)
        )
        shortcuts = result.scalars().all()
        
        return [{
            "action": s.action,
            "shortcut": s.shortcut
        } for s in shortcuts]


# ==================== HELPER FUNCTIONS ====================

async def update_user_stats(session, user_name: str, stat_field: str):
    """Update user statistics"""
    result = await session.execute(select(User).where(User.user_name == user_name))
    user = result.scalars().first()
    
    if not user:
        return
    
    result = await session.execute(
        select(UserStatistic).where(UserStatistic.user_id == user.id)
    )
    stats = result.scalars().first()
    
    if not stats:
        stats = UserStatistic(user_id=user.id)
        session.add(stats)
        await session.flush()

    if hasattr(stats, stat_field):
        setattr(stats, stat_field, getattr(stats, stat_field) + 1)

    stats.last_stats_update = datetime.utcnow()
    await session.commit()


# ==================== BOT API ====================

class BotManager:
    """Manager for bot integrations"""
    def __init__(self):
        self.active_bots: dict[str, dict] = {}
        self.bot_commands: dict[str, list] = {}
        self.bot_events: dict[str, list] = {}
    
    async def dispatch_event(self, event_type: str, data: dict):
        """Dispatch event to all subscribed bots"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotIntegration).where(BotIntegration.enabled == True)
            )
            bots = result.scalars().all()
            
            for bot in bots:
                import json
                config = json.loads(bot.config) if bot.config else {}
                subscribed_events = config.get('events', [])
                
                if event_type in subscribed_events and bot.webhook_url:
                    # Send webhook
                    import aiohttp
                    try:
                        async with aiohttp.ClientSession() as http_session:
                            await http_session.post(
                                bot.webhook_url,
                                json={'event': event_type, 'data': data},
                                headers={'Authorization': f'Bearer {bot.api_key}'}
                            )
                    except Exception as e:
                        print(f"Bot webhook error: {e}")
    
    def register_command(self, bot_name: str, command: str, handler):
        """Register bot command"""
        if bot_name not in self.bot_commands:
            self.bot_commands[bot_name] = []
        self.bot_commands[bot_name].append({'command': command, 'handler': handler})
    
    async def process_command(self, command: str, args: list, user: str, session: AsyncSession):
        """Process bot command"""
        for bot_name, commands in self.bot_commands.items():
            for cmd in commands:
                if cmd['command'] == command:
                    return await cmd['handler'](args, user, session)
        return None

bot_manager = BotManager()


# ==================== BOT API ENDPOINTS ====================

@app.get('/api/bots')
async def get_bots(request: Request):
    """Get all registered bots"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration))
        bots = result.scalars().all()
        
        return [{
            "id": b.id,
            "name": b.name,
            "enabled": b.enabled,
            "webhook_url": b.webhook_url,
            "created_at": b.created_at.isoformat(),
            "has_config": b.config is not None
        } for b in bots]

@app.post('/api/bots/register')
async def register_bot(
    request: Request,
    name: str = Form(...),
    webhook_url: str = Form(None),
    events: str = Form("[]"),  # JSON array
    config: str = Form("{}")   # JSON config
):
    """Register a new bot"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        import secrets
        api_key = f"bot_{secrets.token_hex(32)}"
        
        bot = BotIntegration(
            name=name,
            api_key=api_key,
            webhook_url=webhook_url,
            events=events,
            config=config
        )
        session.add(bot)
        await session.commit()
        
        bot_manager.active_bots[name] = {
            "id": bot.id,
            "api_key": api_key,
            "webhook_url": webhook_url
        }
        
        return {
            "ok": True,
            "bot_id": bot.id,
            "api_key": api_key,
            "message": "Bot zarejestrowany. Zachowaj API Key!"
        }

@app.post('/api/bots/{bot_id}/toggle')
async def toggle_bot(bot_id: int, request: Request):
    """Enable/disable bot"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration).where(BotIntegration.id == bot_id))
        bot = result.scalars().first()
        
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        
        bot.enabled = not bot.enabled
        await session.commit()
        
        return {"ok": True, "enabled": bot.enabled}

@app.delete('/api/bots/{bot_id}')
async def delete_bot(bot_id: int, request: Request):
    """Delete a bot"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration).where(BotIntegration.id == bot_id))
        bot = result.scalars().first()
        
        if bot:
            await session.delete(bot)
            await session.commit()
        
        return {"ok": True}

@app.post('/api/bots/{bot_id}/webhook')
async def update_bot_webhook(
    bot_id: int,
    request: Request,
    webhook_url: str = Form(...)
):
    """Update bot webhook URL"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration).where(BotIntegration.id == bot_id))
        bot = result.scalars().first()
        
        if bot:
            bot.webhook_url = webhook_url
            await session.commit()
        
        return {"ok": True}

@app.get('/api/bots/{bot_id}/logs')
async def get_bot_logs(bot_id: int, request: Request, limit: int = 50):
    """Get bot activity logs"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ActivityLog).where(
                ActivityLog.action.like("BOT_%")
            ).order_by(ActivityLog.created_at.desc()).limit(limit)
        )
        logs = result.scalars().all()
        
        return [{
            "id": l.id,
            "action": l.action,
            "details": l.details,
            "date": l.created_at.isoformat()
        } for l in logs]


# ==================== BOT COMMANDS SYSTEM ====================

@app.post('/api/bots/command')
async def execute_bot_command(
    request: Request,
    command: str = Form(...),
    args: str = Form("[]"),  # JSON array
    chat_with: str = Form(None)
):
    """Execute bot command from chat"""
    import json
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        command_parts = args if args else []
        if isinstance(command_parts, str):
            try:
                command_parts = json.loads(command_parts)
            except:
                command_parts = []
        
        # Process command
        response = await bot_manager.process_command(
            command, command_parts, user_name, session
        )
        
        if response:
            # Send bot response to chat
            if chat_with:
                msg = Message(
                    sender_name="Bot",
                    receiver_name=user_name,
                    text=response.get('text', '')
                )
                session.add(msg)
                await session.commit()
                
                payload = {
                    "type": "message",
                    "sender": "Bot",
                    "text": response.get('text', ''),
                    "id": msg.id
                }
                await manager.send_personal_message(payload, user_name)
            
            return {"ok": True, "response": response}
        
        return {"ok": False, "error": "Komenda nieznaleziona"}


# ==================== BUILT-IN BOT COMMANDS ====================

async def cmd_help(args, user, session):
    return {"text": "Dostępne komendy: /help, /stats, /weather, /quote, /ping, /admin"}

async def cmd_stats(args, user, session):
    result = await session.execute(select(UserStatistic).where(UserStatistic.user_id == user))
    stats = result.scalars().first()
    if stats:
        return {"text": f"📊 Twoje statystyki:\n📝 Wiadomości: {stats.messages_sent}\n📁 Pliki: {stats.files_sent}\n🎤 Głosowe: {stats.voice_messages_sent}\n📊 Ankiety: {stats.polls_created}"}
    return {"text": "Brak statystyk"}

async def cmd_ping(args, user, session):
    import time
    start = time.time()
    await session.execute(select(1))
    latency = int((time.time() - start) * 1000)
    return {"text": f"🏓 Pong! Latencja: {latency}ms"}

async def cmd_quote(args, user, session):
    import random
    quotes = [
        "💬 \"Jedyny sposób na wykonanie wielkiej pracy to kochać to, co się robi.\" - Steve Jobs",
        "💬 \"Przyszłość należy do tych, którzy wierzą w piękno swoich marzeń.\" - Eleanor Roosevelt",
        "💬 \"Nie czekaj. Czas nigdy nie będzie idealny.\" - Napoleon Hill",
        "💬 \"Sukces to nie klucz do szczęścia. Szczęście to klucz do sukcesu.\""
    ]
    return {"text": random.choice(quotes)}

async def cmd_admin(args, user, session):
    result = await session.execute(select(User).where(User.id == user))
    u = result.scalars().first()
    if u and u.is_admin:
        return {"text": "✅ Masz uprawnienia administratora"}
    return {"text": "❌ Nie masz uprawnień administratora"}

async def cmd_weather(args, user, session):
    if len(args) < 1:
        return {"text": "🌡️ Użycie: /weather <miasto>"}
    city = args[0]
    # Mock weather data (in production, call real API)
    import random
    temp = random.randint(-10, 35)
    conditions = ["☀️ Słonecznie", "⛅ Pochmurno", "🌧️ Deszczowo", "❄️ Śnieg", "⛈️ Burza"]
    return {"text": f"🌡️ Pogoda: {city}\n🌡️ Temperatura: {temp}°C\n{random.choice(conditions)}"}

async def cmd_roll(args, user, session):
    import random
    max_val = int(args[0]) if args and args[0].isdigit() else 6
    return {"text": f"🎲 Rzucasz kostką k{max_val}: {random.randint(1, max_val)}"}

async def cmd_coin(args, user, session):
    import random
    result = "Orzeł" if random.random() < 0.5 else "Reszka"
    return {"text": f"🪵 Rzucasz monetą: {result}"}

# Register built-in commands
bot_manager.register_command("system", "help", cmd_help)
bot_manager.register_command("system", "stats", cmd_stats)
bot_manager.register_command("system", "ping", cmd_ping)
bot_manager.register_command("system", "quote", cmd_quote)
bot_manager.register_command("system", "admin", cmd_admin)
bot_manager.register_command("system", "weather", cmd_weather)
bot_manager.register_command("system", "roll", cmd_roll)
bot_manager.register_command("system", "coin", cmd_coin)


# ==================== BOT WEBHOOK RECEIVER ====================

@app.post('/api/bots/webhook/{bot_name}')
async def bot_webhook_receiver(
    bot_name: str,
    request: Request,
    authorization: str = None
):
    """Receive webhook from external bot service"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BotIntegration).where(
                BotIntegration.name == bot_name,
                BotIntegration.enabled == True
            )
        )
        bot = result.scalars().first()
        
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found or disabled")
        
        if authorization != f'Bearer {bot.api_key}':
            raise HTTPException(status_code=401, detail="Invalid API key")
        
        body = await request.json()
        
        # Log bot activity
        log = ActivityLog(
            user_id=None,
            action=f"BOT_{bot_name.upper()}",
            details=str(body),
            ip_address=request.client.host if request.client else None
        )
        session.add(log)
        await session.commit()
        
        # Process bot response
        if 'response' in body:
            return {"ok": True, "processed": True}
        
        return {"ok": True}


# ==================== BOT EVENTS ====================

@app.post('/api/bots/events/subscribe')
async def subscribe_bot_event(
    request: Request,
    bot_id: int = Form(...),
    event: str = Form(...)
):
    """Subscribe bot to an event"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration).where(BotIntegration.id == bot_id))
        bot = result.scalars().first()
        
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        
        import json
        config = json.loads(bot.config) if bot.config else {}
        events = config.get('events', [])
        
        if event not in events:
            events.append(event)
        
        config['events'] = events
        bot.config = json.dumps(config)
        await session.commit()
        
        return {"ok": True, "events": events}

@app.post('/api/bots/events/unsubscribe')
async def unsubscribe_bot_event(
    request: Request,
    bot_id: int = Form(...),
    event: str = Form(...)
):
    """Unsubscribe bot from an event"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration).where(BotIntegration.id == bot_id))
        bot = result.scalars().first()
        
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        
        import json
        config = json.loads(bot.config) if bot.config else {}
        events = config.get('events', [])
        
        if event in events:
            events.remove(event)
        
        config['events'] = events
        bot.config = json.dumps(config)
        await session.commit()
        
        return {"ok": True, "events": events}

@app.get('/api/bots/events')
async def get_bot_events():
    """Get available bot events"""
    return {
        "events": [
            {"name": "message.sent", "description": "Wiadomość wysłana"},
            {"name": "message.received", "description": "Wiadomość otrzymana"},
            {"name": "user.login", "description": "Użytkownik zalogowany"},
            {"name": "user.logout", "description": "Użytkownik wylogowany"},
            {"name": "user.register", "description": "Nowy użytkownik"},
            {"name": "file.upload", "description": "Plik przesłany"},
            {"name": "command.executed", "description": "Komenda wykonana"}
        ]
    }


# ==================== BOT CHAT INTEGRATION ====================

@app.websocket('/ws/bot/{bot_name}')
async def bot_websocket_endpoint(websocket: WebSocket, bot_name: str):
    """WebSocket connection for bots"""
    await websocket.accept()
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BotIntegration).where(
                BotIntegration.name == bot_name,
                BotIntegration.enabled == True
            )
        )
        bot = result.scalars().first()
        
        if not bot:
            await websocket.close(code=4001, reason="Bot not found or disabled")
            return
        
        # Verify API key
        data = await websocket.receive_json()
        if data.get('api_key') != bot.api_key:
            await websocket.close(code=4002, reason="Invalid API key")
            return
        
        bot_manager.active_bots[bot_name]['websocket'] = websocket
        
        try:
            while True:
                data = await websocket.receive_json()
                
                # Process bot message/action
                action = data.get('action')
                
                if action == 'send_message':
                    to = data.get('to')
                    text = data.get('text')
                    
                    if to and text:
                        msg = Message(
                            sender_name=bot_name,
                            receiver_name=to,
                            text=text
                        )
                        session.add(msg)
                        await session.commit()
                        
                        payload = {
                            "type": "message",
                            "sender": bot_name,
                            "text": text,
                            "id": msg.id
                        }
                        await manager.send_personal_message(payload, to)
                        
                        await websocket.send_json({"ok": True, "message_id": msg.id})
                
                elif action == 'get_user':
                    username = data.get('username')
                    result = await session.execute(
                        select(User).where(User.user_name == username)
                    )
                    u = result.scalars().first()
                    
                    if u:
                        await websocket.send_json({
                            "ok": True,
                            "user": {
                                "id": u.id,
                                "username": u.user_name,
                                "status": u.status
                            }
                        })
                    else:
                        await websocket.send_json({"ok": False, "error": "User not found"})
                
                elif action == 'broadcast':
                    text = data.get('text')
                    # Send to all connected users
                    for username in manager.active_connections.keys():
                        msg = Message(
                            sender_name=bot_name,
                            receiver_name=username,
                            text=text
                        )
                        session.add(msg)
                        payload = {
                            "type": "message",
                            "sender": bot_name,
                            "text": text,
                            "id": msg.id
                        }
                        await manager.send_personal_message(payload, username)
                    await session.commit()
                    await websocket.send_json({"ok": True})
                
        except WebSocketDisconnect:
            if bot_name in bot_manager.active_bots:
                del bot_manager.active_bots[bot_name]['websocket']


# ==================== BOT ANALYTICS ====================

@app.get('/api/bots/{bot_id}/analytics')
async def get_bot_analytics(bot_id: int, request: Request, days: int = 7):
    """Get bot analytics"""
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        result = await session.execute(select(BotIntegration).where(BotIntegration.id == bot_id))
        bot = result.scalars().first()
        
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        # Get activity count
        result = await session.execute(
            select(func.count(ActivityLog.id)).where(
                ActivityLog.action.like(f"BOT_{bot.name.upper()}%"),
                ActivityLog.created_at > cutoff
            )
        )
        activity_count = result.scalar()
        
        # Get commands count
        result = await session.execute(
            select(func.count(Message.id)).where(
                Message.sender_name == bot.name,
                Message.created_at > cutoff
            )
        )
        messages_count = result.scalar()
        
        return {
            "bot_name": bot.name,
            "period_days": days,
            "activity_count": activity_count,
            "messages_sent": messages_count,
            "avg_per_day": round(activity_count / days, 2) if days > 0 else 0
        }


# ==================== BOT TEMPLATES ====================

@app.get('/api/bots/templates')
async def get_bot_templates():
    """Get bot templates for quick setup"""
    return {
        "templates": [
            {
                "name": "Welcome Bot",
                "description": "Automatycznie wita nowych użytkowników",
                "events": ["user.register", "user.login"],
                "config": {
                    "welcome_message": "Witaj w naszym czacie! 🎉",
                    "send_rules": True
                }
            },
            {
                "name": "Moderation Bot",
                "description": "Automatyczna moderacja treści",
                "events": ["message.sent"],
                "config": {
                    "auto_delete_profanity": True,
                    "warn_on_violation": True,
                    "max_warnings": 3
                }
            },
            {
                "name": "Notification Bot",
                "description": "Wysyła powiadomienia systemowe",
                "events": [],
                "config": {
                    "broadcast_enabled": True,
                    "scheduled_messages": []
                }
            },
            {
                "name": "Integration Bot",
                "description": "Integracja z zewnętrznymi API",
                "events": ["command.executed"],
                "config": {
                    "external_api_url": "",
                    "api_key": "",
                    "timeout": 30
                }
            }
        ]
    }

@app.post('/api/bots/create-from-template')
async def create_bot_from_template(
    request: Request,
    template_name: str = Form(...),
    bot_name: str = Form(...),
    config: str = Form("{}")
):
    """Create bot from template"""
    import json
    import secrets
    
    user_name = request.session.get("user_name")
    if not user_name:
        raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        
        templates = {
            "Welcome Bot": {"events": ["user.register", "user.login"]},
            "Moderation Bot": {"events": ["message.sent"]},
            "Notification Bot": {"events": []},
            "Integration Bot": {"events": ["command.executed"]}
        }
        
        if template_name not in templates:
            return {"ok": False, "error": "Template not found"}
        
        api_key = f"bot_{secrets.token_hex(32)}"
        template_config = templates[template_name]
        template_config['events'] = template_config.get('events', [])
        
        bot = BotIntegration(
            name=bot_name,
            api_key=api_key,
            webhook_url=None,
            config=json.dumps(template_config),
            enabled=True
        )
        session.add(bot)
        await session.commit()

        return {
            "ok": True,
            "bot_id": bot.id,
            "api_key": api_key
        }


# ==================== IMPORT WSZYSTKICH FUNKCJI ====================
from features_all import router as features_router
app.include_router(features_router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
