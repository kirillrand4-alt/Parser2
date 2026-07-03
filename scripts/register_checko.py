#!/usr/bin/env python3
"""Автоматическая регистрация аккаунта на checko.ru с подтверждением через почту.

Процесс:
  1. Регистрация на https://checko.ru/register
  2. Проверка почты и подтверждение регистрации
  3. Вход в кабинет и получение API ключа
  4. Сохранение ключа в .env файл

Требует переменные окружения:
  - CHECKO_EMAIL: email для регистрации
  - CHECKO_PASSWORD: пароль (минимум 8 символов)
  - MAIL_EMAIL: адрес почты (обычно совпадает с CHECKO_EMAIL)
  - MAIL_PASSWORD: пароль от почты
  - MAIL_IMAP: IMAP сервер (опционально, по умолчанию автоопределение)

Примеры использования:
  export CHECKO_EMAIL=your@example.ru
  export CHECKO_PASSWORD=SecurePass123
  export MAIL_EMAIL=your@example.ru
  export MAIL_PASSWORD=MailPassword
  python scripts/register_checko.py

  # Или для корпоративной почты с явным IMAP:
  export MAIL_IMAP=mail.prokompressor.ru
  python scripts/register_checko.py
"""
from __future__ import annotations

import os
import re
import time
import imaplib
import email
from email.header import decode_header
from urllib.parse import urlparse
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metalparser.browser import DEFAULT_PROFILE  # noqa: E402


def get_imap_server(mail_domain: str) -> str:
    """Определяет IMAP сервер по домену почты."""
    domain = mail_domain.split("@")[-1].lower()

    # Известные провайдеры
    known = {
        "gmail.com": "imap.gmail.com",
        "yandex.ru": "imap.yandex.ru",
        "yandex.com": "imap.yandex.com",
        "mail.ru": "imap.mail.ru",
        "outlook.com": "outlook.office365.com",
        "hotmail.com": "outlook.office365.com",
    }

    if domain in known:
        return known[domain]

    # Если корпоративная почта, пробуем mail.domain.com
    if "." in domain:
        return f"mail.{domain}"

    return "imap.gmail.com"  # fallback


def get_confirmation_link(imap_server: str, email_addr: str, password: str, timeout: int = 300) -> str:
    """Ищет ссылку подтверждения в письме от checko.

    Args:
        imap_server: IMAP сервер (imap.gmail.com, mail.example.com и т.д.)
        email_addr: адрес почты
        password: пароль от почты
        timeout: максимальное время ожидания письма (сек)

    Returns:
        URL ссылки подтверждения или пустая строка если не найдена
    """
    print(f"🔄 Подключаюсь к почте {email_addr} на {imap_server}...")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Подключение к IMAP
            imap = imaplib.IMAP4_SSL(imap_server, 993)
            imap.login(email_addr, password)
            imap.select("INBOX")

            # Ищем письмо от checko
            status, messages = imap.search(None, 'FROM', 'checko')
            if status != 'OK' or not messages[0]:
                print(f"  ⏳ Письмо от checko ещё не пришло, жду... ({int(time.time() - start_time)}сек)")
                imap.close()
                imap.logout()
                time.sleep(5)
                continue

            # Берём последнее письмо
            msg_ids = messages[0].split()
            if not msg_ids:
                print("  ⏳ Письмо не найдено")
                imap.close()
                imap.logout()
                time.sleep(5)
                continue

            latest_email_id = msg_ids[-1]
            status, msg_data = imap.fetch(latest_email_id, "(RFC822)")

            if status != 'OK':
                imap.close()
                imap.logout()
                time.sleep(5)
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            body = ""

            # Достаём текст письма
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    elif part.get_content_type() == "text/html":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

            # Ищем URL в письме
            urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]*', body)
            confirmation_urls = [u for u in urls if 'confirm' in u.lower() or 'verify' in u.lower()]

            imap.close()
            imap.logout()

            if confirmation_urls:
                print(f"✅ Письмо найдено! Ссылка подтверждения: {confirmation_urls[0]}")
                return confirmation_urls[0]
            elif urls:
                # Возвращаем первый URL как fallback
                print(f"✅ Письмо найдено! Используем URL: {urls[0]}")
                return urls[0]
            else:
                print("  ❌ Ссылка в письме не найдена")
                return ""

        except imaplib.IMAP4.error as e:
            if "Authentication failed" in str(e):
                print(f"❌ Ошибка аутентификации на почте: {e}")
                print("   Проверьте email и пароль (для Gmail может потребоваться app password)")
                raise
            print(f"  ⚠️ Ошибка IMAP: {e}, жду...")
            time.sleep(5)
        except Exception as e:
            print(f"  ⚠️ Ошибка: {e}, жду...")
            time.sleep(5)

    print(f"❌ Письмо от checko не пришло за {timeout} секунд")
    return ""


def register_checko(email: str, password: str) -> bool:
    """Регистрирует аккаунт на checko.ru через браузер.

    Args:
        email: адрес для регистрации
        password: пароль (минимум 8 символов)

    Returns:
        True если регистрация успешна
    """
    print(f"📝 Начинаю регистрацию на checko.ru для {email}...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright не установлен. Установите: pip install playwright")
        print("   Затем: playwright install chromium")
        return False

    profile = os.environ.get("CHECKO_PROFILE") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "browser_profile_register"
    )
    os.makedirs(profile, exist_ok=True)

    try:
        with sync_playwright() as p:
            kwargs = {
                "headless": False,
                "locale": "ru-RU",
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            exe = os.environ.get("PLAYWRIGHT_CHROME")
            if exe:
                kwargs["executable_path"] = exe

            ctx = p.chromium.launch_persistent_context(profile, **kwargs)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Переходим на страницу регистрации
            print("🌐 Открываю https://checko.ru/register...")
            page.goto("https://checko.ru/register", wait_until="domcontentloaded")

            # Заполняем форму регистрации
            print(f"✏️  Заполняю форму...")

            # Ищем поля email и пароля (селекторы могут варьироваться)
            email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[id*="email"]']
            password_selectors = ['input[type="password"]', 'input[name="password"]', 'input[id*="password"]']

            email_filled = False
            for selector in email_selectors:
                try:
                    page.fill(selector, email)
                    email_filled = True
                    print(f"  ✓ Email заполнен ({selector})")
                    break
                except:
                    continue

            if not email_filled:
                print("❌ Не удалось найти поле email")
                ctx.close()
                return False

            password_filled = False
            for selector in password_selectors:
                try:
                    page.fill(selector, password)
                    password_filled = True
                    print(f"  ✓ Пароль заполнен ({selector})")
                    break
                except:
                    continue

            if not password_filled:
                print("❌ Не удалось найти поле пароля")
                ctx.close()
                return False

            # Ищем и кликаем кнопку отправки
            submit_selectors = [
                'button:has-text("Зарегистрироваться")',
                'button:has-text("Создать")',
                'button:has-text("Регистрация")',
                'button[type="submit"]',
            ]

            submitted = False
            for selector in submit_selectors:
                try:
                    page.click(selector)
                    submitted = True
                    print(f"✅ Форма отправлена ({selector})")
                    break
                except:
                    continue

            if not submitted:
                print("⚠️  Не удалось найти кнопку отправки. Проверьте форму в открытом окне.")
                print("    После заполнения нажмите Enter для продолжения...")
                input()

            # Ждём ответ сервера
            time.sleep(3)

            # Проверяем успешность
            if "регистра" in page.url.lower() or "confirm" in page.url.lower():
                print("✅ Регистрация отправлена!")
                ctx.close()
                return True
            elif "ошибка" in page.content().lower() or "error" in page.content().lower():
                print("❌ Ошибка при регистрации. Проверьте данные и попробуйте снова.")
                print("   Браузер остался открытым для диагностики.")
                input("Нажмите Enter для закрытия браузера...")
                ctx.close()
                return False
            else:
                print("⚠️  Статус регистрации неясен. Браузер остался открытым.")
                input("Проверьте, прошла ли регистрация. Нажмите Enter для продолжения...")
                ctx.close()
                return True

    except Exception as e:
        print(f"❌ Ошибка при открытии браузера: {e}")
        return False


def get_api_key(email: str, password: str) -> str | None:
    """Заходит в кабинет и получает API ключ.

    Args:
        email: email аккаунта
        password: пароль

    Returns:
        API ключ или None если не удалось получить
    """
    print("🔐 Получаю API ключ...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright не установлен")
        return None

    profile = os.environ.get("CHECKO_PROFILE") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "browser_profile_register"
    )

    try:
        with sync_playwright() as p:
            kwargs = {
                "headless": True,  # headless для получения ключа
                "locale": "ru-RU",
            }
            exe = os.environ.get("PLAYWRIGHT_CHROME")
            if exe:
                kwargs["executable_path"] = exe

            ctx = p.chromium.launch_persistent_context(profile, **kwargs)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Заходим в личный кабинет
            print("🌐 Открываю https://checko.ru/cabinet...")
            page.goto("https://checko.ru/cabinet", wait_until="domcontentloaded")

            # Если требуется вход
            if "login" in page.url.lower():
                print("🔑 Вход в аккаунт...")
                page.fill('input[type="email"]', email)
                page.fill('input[type="password"]', password)
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")

            # Ищем API ключ на странице
            content = page.content()

            # Пробуем разные варианты поиска ключа
            api_key_patterns = [
                r'api[_-]key["\']?\s*[:=]\s*["\']([a-zA-Z0-9]+)["\']',
                r'key["\']?\s*[:=]\s*["\']([a-zA-Z0-9]+)["\']',
                r'<code[^>]*>([a-zA-Z0-9]{20,})</code>',
                r'value=["\']([a-zA-Z0-9]{20,})["\']',
            ]

            api_key = None
            for pattern in api_key_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    api_key = matches[0]
                    break

            ctx.close()

            if api_key:
                print(f"✅ API ключ получен: {api_key[:10]}...{api_key[-4:]}")
                return api_key
            else:
                print("⚠️  API ключ не найден на странице кабинета")
                print("   Возможно, требуется вручную создать ключ в настройках")
                return None

    except Exception as e:
        print(f"❌ Ошибка при получении API ключа: {e}")
        return None


def save_api_key(api_key: str, env_file: str = ".env") -> bool:
    """Сохраняет API ключ в .env файл.

    Args:
        api_key: полученный ключ
        env_file: путь к файлу .env

    Returns:
        True если сохранено успешно
    """
    try:
        # Читаем существующий .env
        content = ""
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                content = f.read()

        # Если CHECKO_API_KEY уже есть, заменяем
        if "CHECKO_API_KEY=" in content:
            content = re.sub(
                r'CHECKO_API_KEY=.*',
                f'CHECKO_API_KEY={api_key}',
                content
            )
        else:
            content += f"\nCHECKO_API_KEY={api_key}\n"

        # Пишем обратно
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"✅ API ключ сохранён в {env_file}")
        return True

    except Exception as e:
        print(f"❌ Ошибка при сохранении .env: {e}")
        return False


def main():
    """Основной цикл регистрации."""
    # Читаем переменные окружения
    checko_email = os.environ.get("CHECKO_EMAIL")
    checko_password = os.environ.get("CHECKO_PASSWORD")
    mail_email = os.environ.get("MAIL_EMAIL")
    mail_password = os.environ.get("MAIL_PASSWORD")
    mail_imap = os.environ.get("MAIL_IMAP")

    # Валидация
    if not all([checko_email, checko_password, mail_email, mail_password]):
        print("❌ Требуются переменные окружения:")
        print("  - CHECKO_EMAIL (email для регистрации на checko)")
        print("  - CHECKO_PASSWORD (пароль, минимум 8 символов)")
        print("  - MAIL_EMAIL (адрес почты для подтверждения)")
        print("  - MAIL_PASSWORD (пароль от почты)")
        print("\nПримеры:")
        print("  export CHECKO_EMAIL=user@example.com")
        print("  export CHECKO_PASSWORD=SecurePass123")
        print("  export MAIL_EMAIL=user@example.com")
        print("  export MAIL_PASSWORD=MailPassword")
        print("  python scripts/register_checko.py")
        return False

    if len(checko_password) < 8:
        print("❌ Пароль должен быть минимум 8 символов")
        return False

    print("=" * 60)
    print("🚀 РЕГИСТРАЦИЯ АККАУНТА CHECKO.RU")
    print("=" * 60)
    print(f"Email:       {checko_email}")
    print(f"Почта:       {mail_email}")
    if mail_imap:
        print(f"IMAP Server: {mail_imap}")
    print("=" * 60)

    # Определяем IMAP сервер если не указан
    if not mail_imap:
        mail_imap = get_imap_server(mail_email)
        print(f"IMAP Server (auto): {mail_imap}")

    # 1. Регистрация
    if not register_checko(checko_email, checko_password):
        print("\n❌ Регистрация не прошла")
        return False

    print()

    # 2. Поиск письма подтверждения
    confirmation_link = get_confirmation_link(
        mail_imap, mail_email, mail_password, timeout=600
    )

    if not confirmation_link:
        print("\n❌ Не удалось получить ссылку подтверждения")
        return False

    print()

    # 3. Переход по ссылке подтверждения
    try:
        from playwright.sync_api import sync_playwright

        print("🔗 Переходу по ссылке подтверждения...")
        with sync_playwright() as p:
            kwargs = {
                "headless": True,
                "locale": "ru-RU",
            }
            exe = os.environ.get("PLAYWRIGHT_CHROME")
            if exe:
                kwargs["executable_path"] = exe

            browser = p.chromium.launch(**kwargs)
            page = browser.new_page()
            page.goto(confirmation_link, wait_until="domcontentloaded")
            time.sleep(2)

            if "confirm" in page.content().lower() or "успешн" in page.content().lower():
                print("✅ Email подтверждён!")
            else:
                print("⚠️  Статус подтверждения неясен")

            browser.close()

    except Exception as e:
        print(f"⚠️  Ошибка при переходе по ссылке: {e}")

    print()

    # 4. Получение API ключа
    api_key = get_api_key(checko_email, checko_password)

    if api_key:
        save_api_key(api_key)
        print("\n" + "=" * 60)
        print("✅ РЕГИСТРАЦИЯ УСПЕШНА!")
        print("=" * 60)
        print(f"API Ключ: {api_key}")
        print(f"Сохранён в .env как CHECKO_API_KEY")
        return True
    else:
        print("\n⚠️  Аккаунт создан, но API ключ не получен автоматически.")
        print("   Получите ключ вручную на https://checko.ru/cabinet/settings/api")
        return True  # регистрация прошла, просто ключ нужен вручную


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
