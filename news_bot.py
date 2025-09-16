import requests
from bs4 import BeautifulSoup
import sqlite3
import asyncio
import re
import random
import time
from datetime import datetime, timedelta
import logging
from telegram import Bot
from telegram.error import TelegramError

# Настройка логирования с временными метками
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class NewsBot:
    def __init__(self, token, channel_id):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.conn = sqlite3.connect('news.db', check_same_thread=False)
        self.cursor = self.conn.cursor()

        # Обновляем структуру базы данных
        self.update_database()

        self.news_queue = []
        self.last_posted_index = 0
        logger.info("✅ База данных инициализирована")

    def update_database(self):
        """Обновляем структуру базы данных"""
        try:
            self.cursor.execute('''CREATE TABLE IF NOT EXISTS posted_news
                                   (id TEXT PRIMARY KEY, 
                                    title TEXT,
                                    link TEXT UNIQUE,  
                                    added_time TIMESTAMP)''')
            self.conn.commit()
            logger.info("✅ База данных инициализирована/обновлена")
        except Exception as e:
            logger.error(f"❌ Ошибка обновления базы данных: {e}")

    def clean_text(self, text):
        """Очистка текста от источников с сохранением абзацев"""
        if not text:
            return ""

        # Разделяем на абзацы
        paragraphs = text.split('\n\n')
        cleaned_paragraphs = []

        for paragraph in paragraphs:
            if not paragraph.strip():
                continue

            # Убираем ВСЕ ссылки
            paragraph = re.sub(r'https?://\S+|www\.\S+', '', paragraph)

            # Убираем упоминания источников в начале абзаца
            paragraph = re.sub(r'^[A-Za-zА-Яа-яёЁ\s]+/[A-Za-zА-Яа-яёЁ\s]+/[A-Za-zА-Яа-яёЁ\s]+,?\s*', '', paragraph)
            paragraph = re.sub(r'^[A-Za-zА-Яа-яёЁ]+\s+[A-Za-zА-Яа-яёЁ]+/,?\s*', '', paragraph)

            # Убираем конкретные источники (только если они стоят отдельно)
            sources_to_remove = [
                '©', 'Веленгурин Владимир', 'Komsomolskaya Pravda', 'East News',
                'РИА Новости', 'ТАСС', 'Instagram', 'Meta', 'Источник:',
                'Фото:', 'Видео:', 'Материал подготовил', 'Автор:', 'Текст:',
            ]

            for source in sources_to_remove:
                # Убираем только если источник стоит отдельно
                paragraph = re.sub(r'\b' + re.escape(source) + r'\b', '', paragraph, flags=re.IGNORECASE)

            # Убираем паттерны с запрещенными соцсетями
            patterns_to_remove = [
                r'\(владелец соцсети компания Meta признана в России экстремистской и запрещена\)',
                r'соцсети.*?запрещена', r'Instagram.*?запрещен', r'Meta.*?экстремистской',
            ]

            for pattern in patterns_to_remove:
                paragraph = re.sub(pattern, '', paragraph, flags=re.IGNORECASE)

            # Очистка от мусора, но сохраняем знаки препинания для абзацев
            paragraph = re.sub(r'[\[\]{}]', '', paragraph)
            paragraph = re.sub(r'<[^>]+>', '', paragraph)
            paragraph = re.sub(r'\S+@\S+', '', paragraph)
            paragraph = re.sub(r'\s+', ' ', paragraph)
            paragraph = paragraph.strip()

            # Убираем предложения со ссылками на другие материалы (только если они в конце)
            sentences = re.split(r'(?<=[.!?])\s+', paragraph)
            cleaned_sentences = []

            for sentence in sentences:
                sentence = sentence.strip()
                if sentence and len(sentence) > 15:
                    # Пропускаем предложения, которые явно являются ссылками на другие материалы
                    lower_sentence = sentence.lower()
                    if not any(phrase in lower_sentence for phrase in [
                        'ранее мы писали', 'читайте также', 'смотрите также',
                        'подробности читайте', 'похожие материалы', 'другие новости',
                        'в другой новости', 'как сообщалось ранее'
                    ]):
                        cleaned_sentences.append(sentence)

            if cleaned_sentences:
                # Собираем абзац обратно
                cleaned_paragraph = ' '.join(cleaned_sentences)
                # Восстанавливаем нормальные пробелы после точек
                cleaned_paragraph = re.sub(r'\.([а-яa-z])', r'. \1', cleaned_paragraph)
                cleaned_paragraphs.append(cleaned_paragraph)

        # Собираем обратно с сохранением абзацев
        result = '\n\n'.join(cleaned_paragraphs)

        return result

    def truncate_at_sentence(self, text, max_length=900):
        """Обрезает текст на последнем законченном предложении"""
        if len(text) <= max_length:
            return text

        for end_char in ['.', '!', '?', '»']:
            end_pos = text.rfind(end_char, 0, max_length)
            if end_pos != -1:
                if end_pos + 1 >= len(text) or text[end_pos + 1] in [' ', '\n', '\r', '\t']:
                    return text[:end_pos + 1].strip()

        last_space = text.rfind(' ', 0, max_length)
        if last_space != -1:
            return text[:last_space].strip() + '...'

        return text[:max_length].strip() + '...'

    def parse_news(self, limit=2):
        """Парсим новости с passion.ru - только две для публикации"""
        sections = [
            "https://www.passion.ru/news/",
            "https://www.passion.ru/news/nash-shoubiz/",
            "https://www.passion.ru/news/eksklyuzivy/",
        ]

        all_news = []
        parsed_count = 0

        for section_url in sections:
            if parsed_count >= limit:
                break

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                }

                response = requests.get(section_url, headers=headers, timeout=15)
                soup = BeautifulSoup(response.text, 'html.parser')

                # Ищем новостные карточки
                news_items = soup.find_all(['article', 'div'],
                                           class_=lambda x: x and any(cls in str(x).lower() for cls in
                                                                      ['news', 'item', 'post', 'article', 'card',
                                                                       'story']),
                                           limit=limit)

                for item in news_items:
                    link = item.find('a', href=re.compile(r'/news/'))
                    if link and link.get('href'):
                        href = link['href']
                        title = link.get_text(strip=True)

                        if title and len(title) > 15:
                            if href.startswith('/'):
                                full_url = 'https://www.passion.ru' + href
                            else:
                                if 'passion.ru' not in href:
                                    continue
                                full_url = href

                            # Проверяем дубликаты
                            if not any(n['link'] == full_url for n in all_news):
                                all_news.append({
                                    'title': title,
                                    'link': full_url
                                })
                                parsed_count += 1
                                if parsed_count >= limit:
                                    break

                if parsed_count >= limit:
                    break

            except Exception as e:
                logger.error(f"Ошибка парсинга {section_url}: {e}")
                continue

        logger.info(f"📰 Найдено новостей: {len(all_news)}")
        return all_news

    def parse_article_content(self, article_url):
        """Парсим контент статьи с сохранением абзацев"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }

            response = requests.get(article_url, headers=headers, timeout=25)
            soup = BeautifulSoup(response.text, 'html.parser')

            # Удаляем ненужные элементы
            for element in soup.find_all(['script', 'style', 'nav', 'footer', 'aside', 'form', 'iframe']):
                element.decompose()

            # Ищем основной контент
            article_text = ""

            # Стратегия 1: Ищем тег article
            article_tag = soup.find('article')
            if article_tag:
                paragraphs = article_tag.find_all('p')
                text_content = []
                for p in paragraphs:
                    text = p.get_text().strip()
                    if len(text) > 40:  # Минимальная длина абзаца
                        text_content.append(text)

                if text_content:
                    article_text = '\n\n'.join(text_content)

            # Стратегия 2: Ищем по классам контента (более мягкие условия)
            if len(article_text) < 200:
                content_classes = ['article-content', 'post-content', 'news-content', 'text-content']
                for class_name in content_classes:
                    content_blocks = soup.find_all('div', class_=lambda x: x and class_name in x)
                    for block in content_blocks:
                        paragraphs = block.find_all('p')
                        text_content = []
                        for p in paragraphs:
                            text = p.get_text().strip()
                            if len(text) > 30:
                                text_content.append(text)

                        if text_content:
                            article_text = '\n\n'.join(text_content)
                            break
                    if article_text:
                        break

            # Тщательная очистка текста с сохранением абзацев
            article_text = self.clean_text(article_text)

            # Ищем изображение
            photo_url = None
            meta_image = soup.find('meta', property='og:image')
            if meta_image and meta_image.get('content'):
                photo_url = meta_image['content']
                if photo_url.startswith('//'):
                    photo_url = 'https:' + photo_url
                elif photo_url.startswith('/'):
                    photo_url = 'https://www.passion.ru' + photo_url

            logger.info(f"📄 Длина контента: {len(article_text)} символов")
            return article_text, photo_url

        except Exception as e:
            logger.error(f"Ошибка парсинга статьи {article_url}: {e}")
            return None, None

    def is_posted(self, link):
        """Проверяем, опубликована ли новость по ссылке"""
        self.cursor.execute("SELECT link FROM posted_news WHERE link=?", (link,))
        return self.cursor.fetchone() is not None

    def mark_as_posted(self, news_id, title, link):
        """Отмечаем новость как опубликованную"""
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute("INSERT INTO posted_news (id, title, link, added_time) VALUES (?, ?, ?, ?)",
                                (news_id, title, link, now))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    async def check_news(self):
        """Проверка новых новостей"""
        logger.info("🔍 НАЧИНАЕМ ПРОВЕРКУ НОВОСТЕЙ...")

        news_items = self.parse_news()
        new_news_count = 0

        for news in news_items:
            # Используем news['link'] для проверки, а не хеш
            if self.is_posted(news['link']):
                logger.info(f"⏭️ Уже опубликовано: {news['title'][:30]}...")
                continue

            news_id = str(abs(hash(news['link'])))

            logger.info(f"📖 Парсим: {news['title'][:40]}...")
            content, photo_url = self.parse_article_content(news['link'])

            if not content or len(content) < 150:
                logger.info(
                    f"❌ Слишком короткий контент ({len(content) if content else 0} символов): {news['title'][:30]}...")
                continue

            if not photo_url:
                logger.info(f"❌ Нет фото: {news['title'][:30]}...")
                continue

            # Подготавливаем контент
            truncated_content = self.truncate_at_sentence(content)

            # Добавляем в очередь
            self.news_queue.append({
                'id': news_id,
                'title': news['title'],
                'link': news['link'],
                'content': truncated_content,
                'photo_url': photo_url
            })

            new_news_count += 1
            logger.info(f"📥 Добавлено в очередь: {news['title'][:50]}...")

        logger.info(f"✅ ПРОВЕРКА ЗАВЕРШЕНА. Новых новостей: {new_news_count}")
        return new_news_count > 0

    async def publish_news(self):
        """Публикация одной новости из очереди"""
        if not self.news_queue:
            logger.info("📭 Очередь пуста")
            return False

        news = self.news_queue.pop(0)  # Берем первую новость из очереди и удаляем ее
        logger.info(f"🎯 ПОДГОТОВКА К ПУБЛИКАЦИИ: {news['title'][:40]}...")

        try:
            # Дополнительная очистка контента
            clean_content = self.clean_text(news['content'])

            message = f"<b>{news['title']}</b>\n\n{clean_content}\n\n#новости"

            # Пытаемся отправить с фото
            try:
                await self.bot.send_photo(
                    chat_id=self.channel_id,
                    photo=news['photo_url'],
                    caption=message[:1024],
                    parse_mode='HTML'
                )
                logger.info("✅ ПОСТ ОПУБЛИКОВАН С ФОТО")

            except Exception as photo_error:
                logger.warning(f"❌ Ошибка фото: {photo_error}")
                await self.bot.send_message(
                    chat_id=self.channel_id,
                    text=message[:4096],
                    parse_mode='HTML'
                )
                logger.info("✅ ПОСТ ОПУБЛИКОВАН БЕЗ ФОТО")

            # Отмечаем как опубликованную
            self.mark_as_posted(news['id'], news['title'], news['link'])

            return True

        except Exception as e:
            logger.error(f"❌ ОШИБКА ПУБЛИКАЦИИ: {e}")
            return False


# --- НОВЫЙ КЛАСС ДЛЯ УПРАВЛЕНИЯ РАСПИСАНИЕМ ---
class NewsScheduler:
    def __init__(self, news_bot):
        self.news_bot = news_bot
        self.working_start_hour = 9
        self.working_end_hour = 21

    async def run(self):
        """Основной цикл работы планировщика"""
        logger.info("🚀 ПЛАНИРОВЩИК ЗАПУЩЕН!")

        while True:
            now = datetime.now()
            current_hour = now.hour

            # Проверяем, находится ли текущее время в рабочем интервале
            if self.working_start_hour <= current_hour < self.working_end_hour:

                # Если очередь пуста, запускаем проверку новостей
                if not self.news_bot.news_queue:
                    logger.info("📭 Очередь пуста. Запускаем проверку...")
                    # Повторяем проверку, пока не найдем новости
                    while not await self.news_bot.check_news():
                        logger.info("😔 Новых новостей нет. Повторная проверка через 10 минут...")
                        await asyncio.sleep(600)  # Ждем 10 минут

                # Публикуем 2 новости
                for i in range(2):
                    logger.info(f"🎯 Начинаем публикацию новости #{i + 1}...")

                    if self.news_bot.news_queue:
                        await self.news_bot.publish_news()
                    else:
                        logger.info("❌ Не удалось найти новости для публикации.")
                        break  # Выходим из цикла публикации, если нет новостей

                    # Пауза между публикациями (2-3 минуты)
                    if i < 1:
                        sleep_time = random.randint(120, 180)
                        logger.info(f"⏸️ Пауза перед следующей публикацией на {sleep_time // 60} минут...")
                        await asyncio.sleep(sleep_time)

                # Ожидаем до начала следующего часа
                now = datetime.now()
                next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                sleep_duration = (next_hour - now).total_seconds()
                logger.info(
                    f"⏳ Публикации за этот час завершены. Следующая серия начнется через {sleep_duration // 60:.0f} минут.")
                await asyncio.sleep(sleep_duration)
            else:
                # В нерабочее время просто ждем до 09:00
                logger.info("🌙 Нерабочее время. Ожидаем до 09:00...")
                now = datetime.now()
                next_run_time = now.replace(hour=self.working_start_hour, minute=0, second=0, microsecond=0)
                if now.hour >= self.working_end_hour:
                    next_run_time += timedelta(days=1)

                sleep_duration = (next_run_time - now).total_seconds()
                logger.info(
                    f"⏳ Ожидание: {sleep_duration // 3600:.0f} часов {(sleep_duration % 3600) // 60:.0f} минут.")
                await asyncio.sleep(sleep_duration)


# Конфигурация
BOT_TOKEN = "8352655660:AAGLuE9ee_qNFimaYWHPdakCw_57kTIfAcI"
CHANNEL_ID = -1002989870351


async def main():
    """Запуск бота"""
    try:
        bot = NewsBot(BOT_TOKEN, CHANNEL_ID)
        scheduler = NewsScheduler(bot)
        await scheduler.run()
    except KeyboardInterrupt:
        logger.info("🛑 БОТ ОСТАНОВЛЕН ПОЛЬЗОВАТЕЛЕМ")
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")


if __name__ == "__main__":
    asyncio.run(main())