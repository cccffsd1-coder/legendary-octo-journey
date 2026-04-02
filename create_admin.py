import asyncio
from sqlalchemy import select
from db import AsyncSessionLocal, User, engine
from security_utils import hash_password

async def create_admin():
    username = input("Введіть логін для адміна: ")
    password = input("Введіть пароль для адміна: ")

    async with AsyncSessionLocal() as session:
        # Перевіряємо, чи існує вже такий користувач
        result = await session.execute(select(User).where(User.user_name == username))
        user = result.scalars().first()

        hashed_pw = hash_password(password)

        if user:
            print(f"Користувач {username} вже існує. Робимо його адміном...")
            user.is_admin = True
            user.password = hashed_pw  # Оновлюємо пароль
        else:
            print(f"Створюємо нового адміна: {username}")
            user = User(
                user_name=username,
                password=hashed_pw,
                is_admin=True
            )
            session.add(user)
        
        await session.commit()
        print("Готово! Користувач тепер має права адміністратора.")

if __name__ == "__main__":
    asyncio.run(create_admin())
