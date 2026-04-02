# ==================== WSZYSTKIE FUNKCJE - ENDPOINTY ====================
# Ten plik zawiera endpointy dla wszystkich 10 faz funkcji

from fastapi import APIRouter, Request, Form, HTTPException, File, UploadFile
from sqlalchemy import select, or_, and_, func, delete, update
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
import json
import secrets

from db import (
    AsyncSessionLocal, User, Message,
    ChatFolder, FolderChat, ArchivedChat, PinnedChat,
    ScheduledMessage, DisappearingMessage,
    SearchIndex, SearchHistory,
    PhotoEdit, VoiceTranscription, MediaGallery,
    UserProfile, UserStory, StoryView, StoryReply,
    SmartReply, Translation, ChatSummary,
    ChatTask, ChatNote, Bookmark,
    SecretChat, AppLock, ScreenshotLog,
    ChatGame, CustomEmoji, MessageEffect,
    CloudBackup, EmailNotification, WebhookIntegration, VoiceMessage
)
from security_utils import hash_password, verify_password

router = APIRouter()

# ==================== FAZA 1: ORGANIZACJA CZATÓW ====================

@router.post("/api/folders/create")
async def create_folder(request: Request, name: str = Form(...), icon: str = Form("📁"), color: str = Form("#3a5bd9")):
    """Create chat folder"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        folder = ChatFolder(user_id=user.id, name=name, icon=icon, color=color)
        session.add(folder)
        await session.commit()
        return {"ok": True, "folder_id": folder.id}

@router.get("/api/folders")
async def get_folders(request: Request):
    """Get user's folders"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(ChatFolder).where(ChatFolder.user_id == user.id).order_by(ChatFolder.position))
        folders = result.scalars().all()
        
        return [{"id": f.id, "name": f.name, "icon": f.icon, "color": f.color} for f in folders]

@router.post("/api/folder/{folder_id}/add-chat")
async def add_chat_to_folder(folder_id: int, request: Request, chat_with: str = Form(...)):
    """Add chat to folder"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        try:
            folder_chat = FolderChat(folder_id=folder_id, chat_with=chat_with)
            session.add(folder_chat)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            return {"ok": False, "error": "Już w folderze"}

@router.post("/api/chat/archive")
async def archive_chat(request: Request, chat_with: str = Form(...)):
    """Archive a chat"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        try:
            archived = ArchivedChat(user_id=user.id, chat_with=chat_with)
            session.add(archived)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            return {"ok": False, "error": "Już zarchiwizowane"}

@router.delete("/api/chat/archive/{chat_with}")
async def unarchive_chat(chat_with: str, request: Request):
    """Unarchive a chat"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(delete(ArchivedChat).where(
            ArchivedChat.user_id == user.id,
            ArchivedChat.chat_with == chat_with
        ))
        await session.commit()
        return {"ok": True}

@router.post("/api/chat/pin")
async def pin_chat(request: Request, chat_with: str = Form(...)):
    """Pin a chat to top"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        # Get max position
        result = await session.execute(
            select(func.max(PinnedChat.position)).where(PinnedChat.user_id == user.id)
        )
        max_pos = result.scalar() or 0
        
        pinned = PinnedChat(user_id=user.id, chat_with=chat_with, position=max_pos + 1)
        session.add(pinned)
        await session.commit()
        return {"ok": True}

@router.delete("/api/chat/unpin/{chat_with}")
async def unpin_chat(chat_with: str, request: Request):
    """Unpin a chat"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(delete(PinnedChat).where(
            PinnedChat.user_id == user.id,
            PinnedChat.chat_with == chat_with
        ))
        await session.commit()
        return {"ok": True}


# ==================== FAZA 2: WIADOMOŚCI CZASOWE ====================

@router.post("/api/message/schedule")
async def schedule_message(
    request: Request,
    receiver: str = Form(...),
    text: str = Form(...),
    scheduled_for: str = Form(...)  # ISO format datetime
):
    """Schedule a message for future delivery"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        scheduled_dt = datetime.fromisoformat(scheduled_for)
        
        msg = ScheduledMessage(
            sender_id=user.id,
            sender_name=user_name,
            receiver_name=receiver,
            text=text,
            scheduled_for=scheduled_dt
        )
        session.add(msg)
        await session.commit()
        
        return {"ok": True, "message_id": msg.id, "scheduled_for": scheduled_for}

@router.get("/api/messages/scheduled")
async def get_scheduled_messages(request: Request):
    """Get user's scheduled messages"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(ScheduledMessage).where(
                ScheduledMessage.sender_id == user.id,
                ScheduledMessage.sent == False
            ).order_by(ScheduledMessage.scheduled_for)
        )
        messages = result.scalars().all()
        
        return [{
            "id": m.id,
            "receiver": m.receiver_name,
            "text": m.text,
            "scheduled_for": m.scheduled_for.isoformat()
        } for m in messages]

@router.delete("/api/message/scheduled/{message_id}")
async def cancel_scheduled_message(message_id: int, request: Request):
    """Cancel a scheduled message"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ScheduledMessage).where(ScheduledMessage.id == message_id))
        await session.commit()
        return {"ok": True}

@router.post("/api/chat/disappearing")
async def set_disappearing_messages(
    request: Request,
    chat_with: str = Form(...),
    seconds: int = Form(...)  # 60, 3600, 86400 etc.
):
    """Enable disappearing messages for a chat"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    # This would be stored as a setting and checked when displaying messages
    return {"ok": True, "chat_with": chat_with, "delete_after": seconds}


# ==================== FAZA 3: WYSZUKIWANIE ====================

@router.get("/api/search/messages")
async def search_messages(q: str, request: Request, chat_with: str = None):
    """Search messages with full-text search"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        # Log search
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        query = select(Message).where(
            or_(
                Message.sender_name == user_name,
                Message.receiver_name == user_name
            ),
            Message.text.like(f"%{q}%")
        ).order_by(Message.created_at.desc()).limit(50)
        
        if chat_with:
            query = query.where(
                or_(
                    and_(Message.sender_name == user_name, Message.receiver_name == chat_with),
                    and_(Message.sender_name == chat_with, Message.receiver_name == user_name)
                )
            )
        
        result = await session.execute(query)
        messages = result.scalars().all()
        
        # Save search history
        search = SearchHistory(user_id=user.id, query=q, results_count=len(messages))
        session.add(search)
        await session.commit()
        
        return [{
            "id": m.id,
            "sender": m.sender_name,
            "text": m.text[:100] + "..." if len(m.text) > 100 else m.text,
            "date": m.created_at.isoformat(),
            "chat_with": m.receiver_name if m.sender_name == user_name else m.sender_name
        } for m in messages]

@router.get("/api/search/history")
async def get_search_history(request: Request):
    """Get user's search history"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(SearchHistory).where(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.searched_at.desc()).limit(20)
        )
        history = result.scalars().all()
        
        return [{"query": h.query, "date": h.searched_at.isoformat()} for h in history]

@router.delete("/api/search/history")
async def clear_search_history(request: Request):
    """Clear search history"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(delete(SearchHistory).where(SearchHistory.user_id == user.id))
        await session.commit()
        return {"ok": True}


# ==================== FAZA 4: MEDIA ====================

@router.get("/api/media/gallery/{chat_with}")
async def get_media_gallery(chat_with: str, request: Request, media_type: str = None):
    """Get shared media in chat"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        query = select(MediaGallery).where(MediaGallery.chat_identifier == chat_with)
        
        if media_type:
            query = query.where(MediaGallery.media_type == media_type)
        
        result = await session.execute(query.order_by(MediaGallery.uploaded_at.desc()).limit(50))
        media = result.scalars().all()
        
        return [{
            "id": m.id,
            "type": m.media_type,
            "url": m.file_url,
            "thumbnail": m.thumbnail_url,
            "date": m.uploaded_at.isoformat()
        } for m in media]

@router.post("/api/voice/transcribe/{voice_id}")
async def transcribe_voice_message(voice_id: int, request: Request):
    """Transcribe voice message to text (mock)"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(VoiceMessage).where(VoiceMessage.id == voice_id))
        voice = result.scalars().first()
        
        if not voice:
            return {"ok": False, "error": "Nie znaleziono"}
        
        # Mock transcription (in production, use speech-to-text API)
        transcription = "[Transkrypcja: To jest przykładowa wiadomość głosowa]"
        
        trans = VoiceTranscription(
            voice_message_id=voice_id,
            transcription=transcription,
            language="pl",
            confidence=0.95
        )
        session.add(trans)
        await session.commit()
        
        return {"ok": True, "transcription": transcription}


# ==================== FAZA 5: SPOŁECZNOŚCIOWE ====================

@router.get("/api/profile/{username}")
async def get_user_profile(username: str, request: Request):
    """Get extended user profile"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()
        
        if not user:
            raise HTTPException(status_code=404)
        
        result = await session.execute(select(UserProfile).where(UserProfile.user_id == user.id))
        profile = result.scalars().first()
        
        return {
            "username": user.user_name,
            "avatar": user.avatar_url,
            "bio": profile.bio if profile else None,
            "website": profile.website if profile else None,
            "location": profile.location if profile else None,
            "instagram": profile.instagram if profile else None,
            "twitter": profile.twitter if profile else None
        }

@router.post("/api/profile/update")
async def update_profile(
    request: Request,
    bio: str = Form(None),
    website: str = Form(None),
    location: str = Form(None),
    instagram: str = Form(None),
    twitter: str = Form(None)
):
    """Update user profile"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(UserProfile).where(UserProfile.user_id == user.id))
        profile = result.scalars().first()
        
        if profile:
            profile.bio = bio
            profile.website = website
            profile.location = location
            profile.instagram = instagram
            profile.twitter = twitter
        else:
            profile = UserProfile(
                user_id=user.id,
                bio=bio,
                website=website,
                location=location,
                instagram=instagram,
                twitter=twitter
            )
            session.add(profile)
        
        await session.commit()
        return {"ok": True}

@router.post("/api/story/create")
async def create_story(
    request: Request,
    media_type: str = Form(...),
    media_url: str = Form(...),
    text_content: str = Form(None)
):
    """Create a 24h story"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        story = UserStory(
            user_id=user.id,
            media_type=media_type,
            media_url=media_url,
            text_content=text_content,
            expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        session.add(story)
        await session.commit()
        
        return {"ok": True, "story_id": story.id}

@router.get("/api/stories")
async def get_stories(request: Request):
    """Get stories from users you follow"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserStory).where(
                UserStory.expires_at > datetime.utcnow()
            ).order_by(UserStory.created_at.desc())
        )
        stories = result.scalars().all()
        
        return [{
            "id": s.id,
            "user": s.user.user_name,
            "media_url": s.media_url,
            "expires_at": s.expires_at.isoformat()
        } for s in stories]


# ==================== FAZA 6: AI ====================

@router.post("/api/ai/translate")
async def translate_message(
    request: Request,
    message_id: int = Form(...),
    target_language: str = Form(...)
):
    """Translate a message (mock)"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).where(Message.id == message_id))
        message = result.scalars().first()
        
        if not message:
            return {"ok": False, "error": "Nie znaleziono"}
        
        # Mock translation (in production, use translation API)
        translations = {
            "en": "[Translated] " + message.text,
            "de": "[Übersetzt] " + message.text,
            "es": "[Traducido] " + message.text,
            "fr": "[Traduit] " + message.text,
        }
        
        translated = translations.get(target_language, "[Przetłumaczono] " + message.text)
        
        trans = Translation(
            message_id=message_id,
            original_text=message.text,
            translated_text=translated,
            target_language=target_language
        )
        session.add(trans)
        await session.commit()
        
        return {"ok": True, "translation": translated}

@router.get("/api/ai/smart-replies/{message_id}")
async def get_smart_replies(message_id: int, request: Request):
    """Get AI smart reply suggestions"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    # Mock smart replies (in production, use AI model)
    return {
        "replies": [
            "👍 OK",
            "Dzięki!",
            "Jasne, że tak!",
            "Może później",
            "😂😂😂"
        ]
    }


# ==================== FAZA 7: PRODUKTYWNOŚĆ ====================

@router.post("/api/tasks/create")
async def create_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(None),
    due_date: str = Form(None),
    chat_with: str = Form(None)
):
    """Create a task"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        task = ChatTask(
            user_id=user.id,
            title=title,
            description=description,
            chat_with=chat_with,
            due_date=datetime.fromisoformat(due_date) if due_date else None
        )
        session.add(task)
        await session.commit()
        
        return {"ok": True, "task_id": task.id}

@router.get("/api/tasks")
async def get_tasks(request: Request, completed: bool = None):
    """Get user's tasks"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        query = select(ChatTask).where(ChatTask.user_id == user.id)
        if completed is not None:
            query = query.where(ChatTask.completed == completed)
        
        result = await session.execute(query.order_by(ChatTask.created_at.desc()))
        tasks = result.scalars().all()
        
        return [{
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "completed": t.completed
        } for t in tasks]

@router.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: int, request: Request):
    """Mark task as complete"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ChatTask).where(ChatTask.id == task_id))
        task = result.scalars().first()
        
        if task:
            task.completed = True
            task.completed_at = datetime.utcnow()
            await session.commit()
        
        return {"ok": True}

@router.post("/api/notes/create")
async def create_note(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    color: str = Form("#ffffff"),
    chat_with: str = Form(None)
):
    """Create a note"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        note = ChatNote(
            user_id=user.id,
            title=title,
            content=content,
            color=color,
            chat_with=chat_with
        )
        session.add(note)
        await session.commit()
        
        return {"ok": True, "note_id": note.id}

@router.get("/api/notes")
async def get_notes(request: Request):
    """Get user's notes"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(ChatNote).where(ChatNote.user_id == user.id)
            .order_by(ChatNote.is_pinned.desc(), ChatNote.updated_at.desc())
        )
        notes = result.scalars().all()
        
        return [{
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "color": n.color,
            "is_pinned": n.is_pinned,
            "updated_at": n.updated_at.isoformat()
        } for n in notes]

@router.post("/api/bookmarks/add")
async def add_bookmark(request: Request, message_id: int = Form(...), note: str = Form(None)):
    """Bookmark a message"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        try:
            bookmark = Bookmark(user_id=user.id, message_id=message_id, note=note)
            session.add(bookmark)
            await session.commit()
            return {"ok": True}
        except IntegrityError:
            await session.rollback()
            return {"ok": False, "error": "Już zapisane"}

@router.get("/api/bookmarks")
async def get_bookmarks(request: Request):
    """Get user's bookmarks"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(Bookmark).where(Bookmark.user_id == user.id)
            .order_by(Bookmark.created_at.desc())
        )
        bookmarks = result.scalars().all()
        
        return [{
            "id": b.id,
            "note": b.note,
            "message_id": b.message_id,
            "date": b.created_at.isoformat()
        } for b in bookmarks]


# ==================== FAZA 8: BEZPIECZEŃSTWO ====================

@router.post("/api/security/app-lock/set")
async def set_app_lock(request: Request, pin: str = Form(...), biometric: bool = Form(False)):
    """Set app lock PIN"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(AppLock).where(AppLock.user_id == user.id))
        lock = result.scalars().first()
        
        if lock:
            lock.pin_hash = hash_password(pin)
            lock.biometric_enabled = biometric
            lock.enabled = True
        else:
            lock = AppLock(
                user_id=user.id,
                pin_hash=hash_password(pin),
                biometric_enabled=biometric,
                enabled=True
            )
            session.add(lock)
        
        await session.commit()
        return {"ok": True}

@router.post("/api/security/app-lock/verify")
async def verify_app_lock(request: Request, pin: str = Form(...)):
    """Verify app lock PIN"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(AppLock).where(AppLock.user_id == user.id))
        lock = result.scalars().first()
        
        if lock and verify_password(pin, lock.pin_hash):
            return {"ok": True, "unlocked": True}
        
        return {"ok": False, "unlocked": False}

@router.delete("/api/security/app-lock")
async def disable_app_lock(request: Request):
    """Disable app lock"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        await session.execute(delete(AppLock).where(AppLock.user_id == user.id))
        await session.commit()
        return {"ok": True}


# ==================== FAZA 9: FUN ====================

@router.post("/api/games/tictactoe/start")
async def start_tictactoe(request: Request, opponent: str = Form(...)):
    """Start tic-tac-toe game"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(User).where(User.user_name == opponent))
        opponent_user = result.scalars().first()
        
        if not opponent_user:
            return {"ok": False, "error": "Użytkownik nie istnieje"}
        
        # Initial game state (3x3 grid)
        initial_state = json.dumps({
            "grid": [[None]*3 for _ in range(3)],
            "current_player": user.id,
            "player1": user.id,
            "player2": opponent_user.id
        })
        
        game = ChatGame(
            game_type="tictactoe",
            player1_id=user.id,
            player2_id=opponent_user.id,
            state=initial_state
        )
        session.add(game)
        await session.commit()
        
        return {"ok": True, "game_id": game.id}

@router.post("/api/games/{game_id}/move")
async def make_move(game_id: int, request: Request, row: int = Form(...), col: int = Form(...)):
    """Make a move in game"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(ChatGame).where(ChatGame.id == game_id))
        game = result.scalars().first()
        
        if not game:
            return {"ok": False, "error": "Gra nie istnieje"}
        
        state = json.loads(game.state)
        
        if state["current_player"] != user.id:
            return {"ok": False, "error": "Nie twój ruch"}
        
        if state["grid"][row][col] is not None:
            return {"ok": False, "error": "Pole zajęte"}
        
        # Make move
        symbol = "X" if user.id == state["player1"] else "O"
        state["grid"][row][col] = symbol
        state["current_player"] = state["player2"] if state["current_player"] == state["player1"] else state["player1"]
        
        game.state = json.dumps(state)
        await session.commit()
        
        return {"ok": True, "state": state}

@router.post("/api/emoji/custom/upload")
async def upload_custom_emoji(
    request: Request,
    name: str = Form(...),
    image_url: str = Form(...)
):
    """Upload custom emoji"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        emoji = CustomEmoji(user_id=user.id, name=name, image_url=image_url)
        session.add(emoji)
        await session.commit()
        
        return {"ok": True, "emoji_id": emoji.id}

@router.get("/api/emoji/custom")
async def get_custom_emoji(request: Request):
    """Get user's custom emoji"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(
            select(CustomEmoji).where(CustomEmoji.user_id == user.id)
        )
        emoji = result.scalars().all()
        
        return [{"id": e.id, "name": e.name, "url": e.image_url} for e in emoji]

@router.post("/api/message/{message_id}/effect")
async def add_message_effect(message_id: int, request: Request, effect_type: str = Form(...)):
    """Add special effect to message"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        effect = MessageEffect(message_id=message_id, effect_type=effect_type)
        session.add(effect)
        await session.commit()
        return {"ok": True}


# ==================== FAZA 10: INTEGRACJE ====================

@router.post("/api/integrations/backup/configure")
async def configure_cloud_backup(
    request: Request,
    provider: str = Form(...),  # google, dropbox, onedrive
    access_token: str = Form(...),
    auto_backup: bool = Form(True),
    frequency: str = Form("daily")
):
    """Configure cloud backup"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(CloudBackup).where(CloudBackup.user_id == user.id))
        backup = result.scalars().first()
        
        if backup:
            backup.provider = provider
            backup.access_token = access_token
            backup.auto_backup = auto_backup
            backup.backup_frequency = frequency
        else:
            backup = CloudBackup(
                user_id=user.id,
                provider=provider,
                access_token=access_token,
                auto_backup=auto_backup,
                backup_frequency=frequency
            )
            session.add(backup)
        
        await session.commit()
        return {"ok": True}

@router.post("/api/integrations/email/configure")
async def configure_email_notifications(
    request: Request,
    email: str = Form(...),
    enabled: bool = Form(True),
    digest_frequency: str = Form("daily")
):
    """Configure email notifications"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        result = await session.execute(select(EmailNotification).where(EmailNotification.user_id == user.id))
        notif = result.scalars().first()
        
        if notif:
            notif.email = email
            notif.enabled = enabled
            notif.digest_frequency = digest_frequency
        else:
            notif = EmailNotification(
                user_id=user.id,
                email=email,
                enabled=enabled,
                digest_frequency=digest_frequency
            )
            session.add(notif)
        
        await session.commit()
        return {"ok": True}

@router.post("/api/integrations/webhook/create")
async def create_webhook_integration(
    request: Request,
    name: str = Form(...),
    webhook_url: str = Form(...),
    events: str = Form("[]")  # JSON array
):
    """Create webhook integration"""
    user_name = request.session.get("user_name")
    if not user_name: raise HTTPException(status_code=401)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_name == user_name))
        user = result.scalars().first()
        
        webhook = WebhookIntegration(
            user_id=user.id,
            name=name,
            webhook_url=webhook_url,
            events=events
        )
        session.add(webhook)
        await session.commit()
        
        return {"ok": True, "webhook_id": webhook.id}
