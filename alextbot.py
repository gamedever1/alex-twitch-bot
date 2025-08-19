# -*- coding: utf-8 -*-

import json, requests, sys, re, asyncio, threading
from time import gmtime, strftime
from datetime import datetime
import openai
import irc.bot
from appJar import gui

# Открываем конфиг из файла, или закрываем программу, если файла нет
CFG_PATH = 'global_vars.json'
try:
    with open(CFG_PATH, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
except FileNotFoundError:
    print('global_vars.json is missing!')
    sys.exit()

# Считываем настройки из конфига global_vars.json
OPENAI_KEY   = cfg.get('openaikey', '')   # ключ OpenAI для GPT-запросов
PROXIES_RAW  = cfg.get('proxies', {"http": "", "https": ""})   # адреса прокси (если пустая строка - не использовать прокси)
VK_TOKEN_DEF = cfg.get('vk_token', '')   # VK-токен для api
VK_GROUP_ID  = str(cfg.get('vk_group_id', '')).strip()   # VK group_id в которой музыка (если пустая строка - api VK отключён)
GAME_LVL     = str(cfg.get('game_lvl', '52'))   # уровень какой-нибудь игры, который берется из конфиг файла
USER         = cfg.get('user', {"username": "", "token": ""})   # Twitch-логин и токен
CHANNELS     = cfg.get('channels', [])   # Twitch-каналы, к которым подключиться

# Готовим словарь прокси только из непустых строк (иначе не используем прокси)
PROXIES = {}
if isinstance(PROXIES_RAW, dict):
    for k, v in PROXIES_RAW.items():
        if isinstance(v, str) and v.strip():
            PROXIES[k] = v.strip()

# Команды по regex: ключ = имя, [0] = regex, [1] = ответ/шаблон
regxs = {
    'tg1':        [r"^!(tg|telegram|тг|телега|телеграм|тегешка)\W{0,2}$", ' ~ Telegram -> @streamer_tg'],
    'donat1':     [r"^!(donate|донат|donation)\W{0,2}$", ' ~ Donate -> donationalerts.com/r/streamer'],
    'mustrack1':  [r"^!(music|song|track|трек|музыка|песня)\W{0,2}$", " ~ Music not found."],   # если VK отключён/пусто пишет not found
    'gamelevel1': [r"((^!level\W{0,2})|(^!lvl\W{0,2}))$", " ~ Genshin Level -> " + GAME_LVL], # Genshin можно поменять на любую другую игру, число уровня берется из конфига
}

# Антиспам: интервал ответов в секундах
GLOBAL_MSG_TIMEOUT = 1
last_msg_times = {}

# GPT-запросы с таймаутом (без аннотаций типов, совместимо с Py3.7/3.10)
async def chatgpt_request(prompt):   # отправляет запрос в OpenAI и возвращает ответ либо None при ошибке
    openai.api_key = OPENAI_KEY
    sess = requests.Session()
    if PROXIES:
        sess.proxies.update(PROXIES)   # подставляем прокси, если заданы
    try:
        r = sess.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer {}".format(openai.api_key), "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": " Ты - ассистент чат-бот на стриме, отвечающий на вопросы об известных людях. Отвечай кратко и без лишнего."},
                    {"role": "system", "content": "Основная информация о стримере Buster: Вячеслав Андреевич Леонтьев, родился 25 февраля 1997 года, Москва, стримит на Twitch."},
                    {"role": "user", "content": prompt}
                ]
            },
            timeout=25
        )
        if not r.ok:
            print("GPT: HTTP {} cant get response.".format(r.status_code))
            return None
        data = r.json()   # парсим JSON-ответ
        msg = data.get('choices', [{}])[0].get('message', {}).get('content')   # достаём текст
        if not msg:
            print("GPT: empty response or wrong JSON.")
            return None
        return msg
    except requests.RequestException as ex:
        print("GPT: network error {}".format(ex))
        return None
    except ValueError as ex:
        print("GPT: JSON error {}".format(ex))
        return None
    except Exception as ex:
        print("GPT: unexpected error {}".format(ex))
        return None

# Создаём GUI: окно, вкладки, поле ввода и кнопка отправки
app = gui("AlextBot IRC Client", "700x300"); app.setBg("#444444")

def create_tab(target, msg=None):   # создаёт вкладку канала/пользователя
    app.startTab(target); app.setBg("#454545")
    app.addListBox(target, [msg], 0, 0, 3); app.addLabelEntry(target, 2, 0)
    app.addNamedButton("Send", target, send_message, 2, 1); app.stopTab()

def clear_entity(target):   # очищает поле ввода
    app.clearEntry(target)

def send_message(button):   # отправляет сообщение из GUI в чат
    msg = app.getEntry(button)
    if msg and msg.strip():
        app.addListItem(button, "{} {}: {}".format(strftime("%H:%M", gmtime()), USER["username"], msg))   # логируем в GUI
        clear_entity(button); irc.connection.privmsg(button, msg)   # шлём в IRC

# Подключение к Twitch, лог чата, ответы по regex и спец.обработка ИИ или запросов муз.трека
class AlextBot(irc.bot.SingleServerIRCBot):   # класс IRC-бота
    def __init__(self):   # подключаемся к Twitch
        irc.bot.SingleServerIRCBot.__init__(self, [('irc.chat.twitch.tv', 6667, 'oauth:{}'.format(USER["token"]))], USER["username"], USER["username"])
        self.vk_api_token = VK_TOKEN_DEF   # берём VK-токен из конфига

    def on_welcome(self, c, e):   # при успешном подключении
        try:
            app.startTabbedFrame("IRC"); app.setTabbedFrameTabExpand("IRC", expand=True)
            app.setFont(12); app.setStretch("both")
            for ch in CHANNELS:
                print('Joining', ch)
                c.cap('REQ', ':twitch.tv/membership'); c.cap('REQ', ':twitch.tv/tags'); c.cap('REQ', ':twitch.tv/commands')   # включаем расширения Twitch IRC
                create_tab(ch, "Joined " + ch); c.join(ch)   # создаём вкладку и входим на канал
            app.stopTabbedFrame()
        except:
            pass

    def get_vk_music(self):   # получает текущий трек из VK, '' если нет/ошибка
        if not VK_GROUP_ID or not self.vk_api_token:
            return ''   # выходим, если VK отключён
        try:
            r = requests.get(
                "https://api.vk.com/method/status.get",
                params={"group_id": VK_GROUP_ID, "access_token": self.vk_api_token, "v": "5.131"},
                timeout=10
            )
            resp = r.json().get('response', {})
            if 'audio' in resp:
                return resp.get('text', '') or ''   # если ответ содержит аудио берём text
            if resp.get('text'):
                return resp['text']   # иначе просто text
        except requests.RequestException as ex:
            print('VK API ERROR (network):', ex)
        except ValueError as ex:
            print('VK API ERROR (json):', ex)
        except Exception as ex:
            print('VK API ERROR (other):', ex)
        return ''

    def onmsg_regx_thread(self, c, e):   # обработка входящего сообщения (regex, команды, GPT)
        msg = e.arguments[0]; chan = e.target; first = e.arguments[0][:1]

        meta = {'username': None, 'badges': None, 'mod': '0'}
        for t in e.tags:
            k = t.get('key'); v = t.get('value')
            if k == 'display-name':
                meta['username'] = v
            elif k == 'badges':
                meta['badges'] = v
            elif k == 'mod':
                meta['mod'] = v
        if meta['badges'] and 'broadcaster' in meta['badges']:
            meta['mod'] = '1'

        do_auto = True
        if meta['mod'] == '1':
            do_auto = first in ('!', '=', '+', '-') or (first == '@' and ('=' in msg or '!' in msg))   # модераторам — только явные команды

        if meta['username'] in last_msg_times:
            if datetime.now().timestamp() - last_msg_times[meta['username']] < GLOBAL_MSG_TIMEOUT:
                do_auto = False
            else:
                last_msg_times.pop(meta['username'], None)

        if '~' in msg:
            do_auto = False

        # Обрабатываем !ai / !ии: шлём запрос к GPT
        low = msg.lower()
        if first == '!' and ('!ai ' in low or '!ии ' in low):
            prompt = msg[msg.find(' ') + 1:]; ans = "~ "
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            try:
                gpt = loop.run_until_complete(chatgpt_request(prompt))
            except Exception as ex:
                print("GPT: cant get response (loop error) {}".format(ex))
                gpt = None
            finally:
                try:
                    loop.close()
                except:
                    pass

            if not gpt:
                print("GPT: cant get response.")
            else:
                cleaned = re.sub(r'[ \t\n\r\f\v]+', ' ', gpt.replace('\r', '').replace('\n', '')).strip()
                ans += cleaned
                ans = ans.encode('utf-8')[:380].decode('utf-8', 'ignore')
                c.privmsg(chan, ans)

            do_auto = False

        # Обрабатываем @реплай
        reply_to, tail = ('', msg)
        if first == '@':
            sp = msg.find(' ')
            reply_to = msg[:sp] if sp > 0 else ''
            tail = msg[sp + 1:] if sp > 0 else ''

        # Подбираем автоответ по regex
        if do_auto:
            for key, val in regxs.items():
                pattern, text = val[0], val[1]
                if re.match(pattern, tail, re.IGNORECASE | re.UNICODE):
                    ans = (reply_to + ' ' if reply_to else '') + text
                    if key == 'mustrack1':
                        mus = self.get_vk_music()
                        if mus:
                            ans = (reply_to + ' ' if reply_to else '') + ('~ Music -> ' + mus)
                    if text:
                        c.privmsg(chan, ans)
                        last_msg_times[meta['username']] = datetime.now().timestamp()
                    break

    def on_pubmsg(self, c, e):   # логируем публичные сообщения в GUI
        threading.Thread(target=self.onmsg_regx_thread, args=(c, e), daemon=True).start()
        try:
            app.addListItem(e.target, "{} {}: {}".format(strftime("%H:%M", gmtime()), e.source.nick, e.arguments[0]))
        except:
            print('Symbol error TKINTER unsupported!!!')

    def on_privmsg(self, c, e):   # показываем приватные сообщения во вкладке
        try:
            app.openTabbedFrame("IRC")
            create_tab(e.source.nick, "{} {}: {}".format(strftime("%H:%M", gmtime()), e.source.nick, e.arguments[0]))
            app.stopTabbedFrame()
        except:
            pass

# Запуск бота, если нет Twitch-логина/токена, запросить их в консоли сохранить в конфиг
irc = AlextBot()
if not USER.get("username") or not USER.get("token"):
    def gui_login(btn):   # окно логина для ввода данных Twitch
        name = app.getEntry("Name"); tok = app.getEntry("Token")
        if name and tok:
            cfg['user'] = {"username": name, "token": tok}
            with open(CFG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            sys.exit()
    app.startSubWindow("GUILogin", title="Login", modal=True)
    app.addLabelEntry("Name"); app.addLabelSecretEntry("Token"); app.setFocus("Name"); app.addButtons(["Login"], gui_login)
    app.stopSubWindow(); app.showSubWindow("GUILogin")
else:
    app.thread(irc.start)

app.go()   # запускаем GUI-цикл
