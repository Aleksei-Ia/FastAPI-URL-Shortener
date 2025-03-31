redisimport uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Request, Response, Form
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import redis
import uuid

from apscheduler.schedulers.background import BackgroundScheduler

from .database import SessionLocal, engine
from .models import Base, Link, User
from .schemas import (LinkInfo, LinkStats, UserCreate)
from .auth import (
    create_access_token,
    get_current_user,
    get_user_by_token,
    hash_password,
    verify_password
)

app = FastAPI(title="URL Shortener (with APScheduler, timezone-aware)")

Base.metadata.create_all(bind=engine)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)

UNREGISTERED_LINK_MAX_AGE_DAYS = 2

optional_oauth2 = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Планировщик APScheduler 

scheduler = BackgroundScheduler()

def delete_expired_links():
    """
    Удаляет все ссылки, у которых expires_at < сейчас (в UTC).
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired_links = db.query(Link).filter(
            Link.expires_at.isnot(None),
            Link.expires_at < now
        ).all()
        for link in expired_links:
            short_code = link.short_code
            db.delete(link)
            redis_client.delete(short_code)
        if expired_links:
            db.commit()
    finally:
        db.close()

@app.on_event("startup")
def start_scheduler():
    """
    Запускаем планировщик на старте приложения.
    Каждые 5 минут будет вызываться delete_expired_links.
    """
    scheduler.add_job(delete_expired_links, "interval", minutes=5, misfire_grace_time=60)
    scheduler.start()

@app.on_event("shutdown")
def shutdown_scheduler():
    """
    Останавливаем планировщик при остановке приложения.
    """
    scheduler.shutdown()


def cleanup_expired_unregistered_links(db: Session):
    """
    Удаляем все гостевые ссылки (user_id=None),
    если их не использовали более 2 дней.
    """
    now = datetime.now(timezone.utc)
    two_days_ago = now - timedelta(days=UNREGISTERED_LINK_MAX_AGE_DAYS)
    unregistered = db.query(Link).filter(
        Link.user_id.is_(None),
        Link.last_accessed < two_days_ago
    ).all()
    for link in unregistered:
        db.delete(link)
        redis_client.delete(link.short_code)
    if unregistered:
        db.commit()

@app.middleware("http")
async def cleanup_middleware(request: Request, call_next):
    """
    Перед каждым запросом чистим expired гостевые ссылки (по last_accessed).
    Отдельно, каждые 5 минут, APScheduler удаляет ссылки, истёкшие по expires_at.
    """
    db = SessionLocal()
    try:
        cleanup_expired_unregistered_links(db)
    finally:
        db.close()

    response = await call_next(request)
    return response


# HTML-Форма

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def show_form():
    """
    Показывает HTML-форму для создания ссылки.
    """
    html_content = """
    <html>
      <head><title>Создать короткую ссылку</title></head>
      <body>
        <h1>Создать короткую ссылку (гостевая)</h1>
        <form action="/links/shorten" method="post">
          <label>Длинная ссылка: <input type="text" name="original_url" /></label><br><br>
          <label>Кастомный алиас (необязательно): <input type="text" name="custom_alias" /></label><br><br>
          <label>Дата истечения (UTC, формата YYYY-MM-DD HH:MM, необязательно):
            <input type="text" name="expires_at" placeholder="2025-12-31 23:59" />
          </label><br><br>
          <button type="submit">Создать</button>
        </form>
        <p>Откройте <a href="/docs">/docs</a> для REST-эндпойнтов</p>
      </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.post("/links/shorten", response_class=HTMLResponse)
def create_short_link(
    original_url: str = Form(...),
    custom_alias: Optional[str] = Form(None),
    expires_at: Optional[str] = Form(None),
    token: Optional[str] = Depends(optional_oauth2),
    db: Session = Depends(get_db)
):
    """
    Создаёт короткую ссылку через HTML-форму. expires_at вводить в UTC (YYYY-MM-DD HH:MM).
    """
    user = get_user_by_token(db, token)
    user_id = user.id if user else None

    expires_datetime = None
    if expires_at:
        try:
            naive_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M")
            expires_datetime = naive_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Неверный формат даты"
            )
    if custom_alias:
        existing = db.query(Link).filter(Link.short_code == custom_alias).first()
        if existing:
            return HTMLResponse("<h2>Ошибка: Алиас уже занят!</h2>", status_code=400)
        short_code = custom_alias
    else:
        short_code = str(uuid.uuid4())[:8]

    now_utc = datetime.now(timezone.utc)
    new_link = Link(
        short_code=short_code,
        original_url=original_url,
        user_id=user_id,
        created_at=now_utc,
        last_accessed=now_utc,
        expires_at=expires_datetime
    )
    db.add(new_link)
    db.commit()
    db.refresh(new_link)

    redis_client.set(short_code, new_link.original_url)

    short_url = f"http://127.0.0.1:8000/links/{short_code}"
    html_result = f"""
    <html>
      <body>
        <h2>Короткая ссылка создана!</h2>
        <p>Short code: {short_code}</p>
        <p>Ссылка: <a href="{short_url}">{short_url}</a></p>
        <p>{short_url}<p>
      </body>
    </html>
    """
    return HTMLResponse(html_result)

# Регистрация

@app.post("/register")
def register_user(user_data: UserCreate, db: Session = Depends(get_db)):
    """
    Регистрирует нового пользователя.
    """
    existing_user = db.query(User).filter(User.username == user_data.username).first()
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Пользователь с таким именем уже существует"
        )
    new_user = User(
        username=user_data.username,
        hashed_password=hash_password(user_data.password),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "Регистрация прошла успешно."}

# OAuth2

@app.post("/token", include_in_schema=False)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    """
    Эндпойнт для получения JWT-токена.
    """
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверные учетные данные")
    access_token = create_access_token({"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

# Поиск по original_url

@app.get("/links/search")
def search_links_by_original_url(original_url: str, db: Session = Depends(get_db)):
    """
    Ищет доступные short ссылки по совпадению c original_url.
    """
    links = db.query(Link).filter(Link.original_url == original_url).all()
    return [
        {
            "short_code": l.short_code,
            "original_url": l.original_url,
            "user_id": l.user_id,
            "created_at": l.created_at,
            "expires_at": l.expires_at
        }
        for l in links
    ]

# Редирект

@app.get("/links/{short_code}")
def redirect_link(short_code: str, db: Session = Depends(get_db)):
    """
    Принимает короткий код, делает редирект (HTTP 307).
    """
    cached_url = redis_client.get(short_code)
    if cached_url:
        original_url = cached_url.decode("utf-8")
        link_obj = db.query(Link).filter(Link.short_code == short_code).first()
        if link_obj:
            link_obj.last_accessed = datetime.now(timezone.utc)
            link_obj.click_count += 1
            db.commit()
        return Response(status_code=307, headers={"Location": original_url})

    link_obj = db.query(Link).filter(Link.short_code == short_code).first()
    if not link_obj:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    now_utc = datetime.now(timezone.utc)
    if link_obj.expires_at and link_obj.expires_at < now_utc:
        raise HTTPException(status_code=410, detail="Срок действия ссылки истёк")

    link_obj.last_accessed = now_utc
    link_obj.click_count += 1
    db.commit()
    redis_client.set(short_code, link_obj.original_url)

    return Response(status_code=307, headers={"Location": link_obj.original_url})

# Статистика ссылки

@app.get("/links/{short_code}/stats", response_model=LinkStats)
def get_link_stats(short_code: str, db: Session = Depends(get_db)):
    """
    Возвращает статистику по ссылке.
    """
    link_obj = db.query(Link).filter(Link.short_code == short_code).first()
    if not link_obj:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    return LinkStats(
        short_code=link_obj.short_code,
        original_url=link_obj.original_url,
        created_at=link_obj.created_at,
        last_accessed=link_obj.last_accessed,
        click_count=link_obj.click_count,
        expires_at=link_obj.expires_at,
        user_id=link_obj.user_id
    )

# Удаление

@app.delete("/links/{short_code}")
def delete_link(
    short_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Удаляет ссылку.
    """
    link_obj = db.query(Link).filter(Link.short_code == short_code).first()
    if not link_obj:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    if not link_obj.user_id:
        raise HTTPException(
            status_code=403,
            detail="Ссылка гостевая и удаляется только авто-очисткой"
        )
    if link_obj.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Вы не являетесь владельцем данной ссылки"
        )

    db.delete(link_obj)
    db.commit()
    redis_client.delete(short_code)
    return {"message": f"Ссылка '{short_code}' удалена"}

# Смена short_code

@app.put("/links/{short_code}", response_model=LinkInfo)
def reassign_code(
    short_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Меняет short_code на новый random.
    """
    link_obj = db.query(Link).filter(Link.short_code == short_code).first()
    if not link_obj:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")

    if not link_obj.user_id or link_obj.user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Вы не являетесь владельцем данной ссылки"
        )

    new_code = str(uuid.uuid4())[:8]
    existing = db.query(Link).filter(Link.short_code == new_code).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Случайно сгенерированный код оказался занят, попробуйте снова"
        )

    redis_client.delete(short_code)
    link_obj.short_code = new_code
    db.commit()
    db.refresh(link_obj)
    redis_client.set(new_code, link_obj.original_url)

    return LinkInfo(
        short_code=link_obj.short_code,
        original_url=link_obj.original_url,
        created_at=link_obj.created_at,
        expires_at=link_obj.expires_at,
        user_id=link_obj.user_id
    )

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
