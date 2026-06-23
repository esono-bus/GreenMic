import speech_recognition as sr
import keyboard
import pyaudio
import time
import threading
import pystray
from PIL import Image, ImageDraw
import os
import pyperclip
import ctypes
import sys
import tkinter as tk
from tkinter import ttk
import logging
import concurrent.futures
import json
import re

# ベースパス（exe化・通常起動の両対応）
if getattr(sys, 'frozen', False):
    BASE_PATH = os.path.dirname(sys.executable)
    # インストール先（Program Files）は書き込み不可のため %APPDATA% を使用
    DATA_PATH = os.path.join(os.environ.get('APPDATA', BASE_PATH), 'GreenMic')
    os.makedirs(DATA_PATH, exist_ok=True)
else:
    BASE_PATH = os.path.dirname(os.path.abspath(__file__))
    DATA_PATH = BASE_PATH

# ログ設定
logging.basicConfig(
    filename=os.path.join(DATA_PATH, 'greenmic.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)

# 二重起動防止
mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "VoiceInputApp_Mutex")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

# -------------------------------------------------------
# 設定管理
# -------------------------------------------------------
CONFIG_PATH = os.path.join(DATA_PATH, 'greenmic_config.json')

DEFAULT_CONFIG = {
    "hotkey": "right ctrl",
    "language": "ja-JP",
    "max_record_seconds": 30,
    "recognize_timeout": 10,
    "auto_punctuation": True
}

LANGUAGE_OPTIONS = {
    "日本語": "ja-JP",
    "English": "en-US",
    "中文": "zh-CN",
    "한국어": "ko-KR",
    "Français": "fr-FR",
    "Deutsch": "de-DE",
    "Español": "es-ES",
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                return {**DEFAULT_CONFIG, **loaded}
        except Exception as e:
            logging.error(f"設定読み込みエラー: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logging.info(f"設定保存完了: {cfg}")
    except Exception as e:
        logging.error(f"設定保存エラー: {e}")

config = load_config()

# -------------------------------------------------------
# グローバル変数
# -------------------------------------------------------
is_recording_key_pressed = False
show_help_flag     = False
show_settings_flag = False
capturing_hotkey   = False   # ホットキー設定中は録音を一時停止
_hook_active = False
_hook = None

# -------------------------------------------------------
# キーボードフック
# -------------------------------------------------------
def get_hotkey():
    return config.get('hotkey', 'right ctrl')

def on_key_event(event):
    global is_recording_key_pressed
    if capturing_hotkey:
        return
    try:
        hotkey = get_hotkey()
        # 'right ctrl' ↔ 'right control' 両方を認識
        aliases = {hotkey}
        if 'ctrl' in hotkey and 'control' not in hotkey:
            aliases.add(hotkey.replace('ctrl', 'control'))
        elif 'control' in hotkey:
            aliases.add(hotkey.replace('control', 'ctrl'))

        if event.name in aliases:
            if event.event_type == keyboard.KEY_DOWN:
                is_recording_key_pressed = True
            elif event.event_type == keyboard.KEY_UP:
                is_recording_key_pressed = False
    except Exception as e:
        logging.warning(f"キーイベント処理エラー: {e}")

def setup_keyboard_hook():
    global _hook_active, _hook
    try:
        if _hook is not None:
            keyboard.unhook(_hook)
        _hook = keyboard.hook(on_key_event)
        _hook_active = True
        logging.info(f"キーボードフック設定完了: {get_hotkey()}")
    except Exception as e:
        logging.error(f"キーボードフック設定失敗: {e}")
        _hook_active = False

def poll_hotkey():
    """フック使用不可時のポーリングフォールバック"""
    global is_recording_key_pressed
    while True:
        if not capturing_hotkey:
            try:
                pressed = keyboard.is_pressed(get_hotkey())
                if pressed != is_recording_key_pressed:
                    is_recording_key_pressed = pressed
            except Exception:
                pass
        time.sleep(0.02)

setup_keyboard_hook()
if not _hook_active:
    logging.warning("フック使用不可のためポーリングモードで起動")
    poll_thread = threading.Thread(target=poll_hotkey, daemon=True)
    poll_thread.start()

# -------------------------------------------------------
# 音声認識（タイムアウト付き）
# -------------------------------------------------------
# -------------------------------------------------------
# 句読点自動挿入
# -------------------------------------------------------
_PUNCT_SKIP = set('。！？!?…、')

# ① 音声認識が接続語の途中にスペースを入れるケースを事前修正
#    例: 「けれど も」→「けれども」 / 「です が」→「ですが」
_RE_SPLIT_FIX = re.compile(
    r'(けれど)\s+(も)'
    r'|(それ)\s+(から)'
    r'|(そ)\s+(して)'
    r'|(しか)\s+(し)'
    r'|(ただ)\s+(し)'
    r'|(つま)\s+(り)'
    r'|(なぜ)\s+(なら)'
    r'|(です)\s+([がけ])'        # 「です が」→「ですが」/ 「です け」→「ですけ」
    r'|(ので)\s+(す|した|して)'   # 「ので す」→「のです」/ 「ので した」→「のでした」
)

def _fix_split(m: re.Match) -> str:
    """マッチした分割語をスペースなしで結合"""
    return ''.join(g for g in m.groups() if g is not None)

# ② 節末パターン（語の直後に読点を挿入）
#    ※ 長い形を先に書く（'のですが' > 'のですけれど' > 'ですが' の順）
#    ※ 「のです。」「のでした。」への誤挿入防止: ので(?![すし])
_RE_AFTER = re.compile(
    r'(のですけれども|のですけれど|のですけども|のですけど'  # のです+逆接
    r'|のですが'                                            # のですが
    r'|けれども|けれど|けども|けど'
    r'|ですけれども|ですけれど|ですけども|ですけど'
    r'|ですが'
    r'|ものの|ながら'
    r'|ので(?![すし])|のに'    # のです/のでした は除外
    r'|しかし|ただし|ところが'
    r'|それから|そして'
    r'|ちなみに|つまり|すなわち|なぜなら'
    r')(?![、。！？!?…])'
)

# ③ 動詞/形容詞/だ + から パターン（「これから」「どこから」と区別）
_RE_KARA = re.compile(r'([たいだ]から)(?![、。！？!?…])')

def add_punctuation(text: str) -> str:
    """認識テキストに句読点（。・、）を自動挿入する"""
    if not text:
        return text

    # Step1: 日本語文字間のスペースを除去（音声認識のアーティファクト）
    #        例: 「けれど も」→「けれども」 / 「ただし 来月」→「ただし来月」
    #        ※ 英単語や英数字の前後のスペースは保持する
    text = re.sub(
        r'([぀-鿿＀-￯])\s+([぀-鿿＀-￯])',
        r'\1\2', text
    )

    # Step2: 節末パターン後に読点を挿入
    text = _RE_AFTER.sub(r'\1、', text)

    # Step3: から（節末）後に読点を挿入
    text = _RE_KARA.sub(r'\1、', text)

    # Step4: 句読点の後に残った余分なスペースを除去
    text = re.sub(r'([、。！？])\s+', r'\1', text)

    # Step5: 文末句点がなければ付与
    if text[-1] not in _PUNCT_SKIP:
        text += '。'

    return text


def recognize_with_timeout(recognizer, audio_data, timeout, language):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            recognizer.recognize_google, audio_data, language=language
        )
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logging.warning(f"音声認識タイムアウト（{timeout}秒）")
            return None
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            logging.error(f"音声認識APIエラー: {e}")
            return None

# -------------------------------------------------------
# 設定ウィンドウ
# -------------------------------------------------------
# tkinterのkeysym → keyboardライブラリ形式 変換マップ
KEYSYM_MAP = {
    'control_l': 'left ctrl',   'control_r': 'right ctrl',
    'alt_l':     'left alt',    'alt_r':     'right alt',
    'shift_l':   'left shift',  'shift_r':   'right shift',
    'super_l':   'left windows','super_r':   'right windows',
    'caps_lock': 'caps lock',   'scroll_lock': 'scroll lock',
    'num_lock':  'num lock',    'pause':     'pause',
    'print':     'print screen',
    'insert':    'insert',      'delete':    'delete',
    'home':      'home',        'end':       'end',
    'prior':     'page up',     'next':      'page down',
    'return':    'enter',       'space':     'space',
    'tab':       'tab',         'escape':    'escape',
    'f1':  'f1',  'f2':  'f2',  'f3':  'f3',  'f4':  'f4',
    'f5':  'f5',  'f6':  'f6',  'f7':  'f7',  'f8':  'f8',
    'f9':  'f9',  'f10': 'f10', 'f11': 'f11', 'f12': 'f12',
}

def open_settings_window(root):
    global config, capturing_hotkey

    win = tk.Toplevel(root)
    win.title("GreenMic 設定")
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    win.geometry(f"400x370+{sw//2 - 200}+{sh//2 - 185}")
    win.resizable(False, False)
    win.attributes('-topmost', True)

    # ---- ホットキー設定 ----
    hotkey_frame = tk.LabelFrame(win, text=" ホットキー ",
                                  font=("メイリオ", 10, "bold"), padx=10, pady=8)
    hotkey_frame.pack(fill='x', padx=15, pady=(15, 8))

    current_hotkey_var = tk.StringVar(value=config.get('hotkey', 'right ctrl'))
    is_capturing = [False]

    hotkey_entry = tk.Entry(hotkey_frame, textvariable=current_hotkey_var,
                             width=22, font=("メイリオ", 10),
                             state='readonly', readonlybackground='#f0f0f0')
    hotkey_entry.grid(row=0, column=0, padx=(0, 10))

    capture_btn = tk.Button(hotkey_frame, text="変更", font=("メイリオ", 9), width=8)
    capture_btn.grid(row=0, column=1)

    capture_label = tk.Label(hotkey_frame, text="", font=("メイリオ", 8), fg="gray")
    capture_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

    def start_capture():
        global capturing_hotkey
        if is_capturing[0]:
            return
        is_capturing[0] = True
        capturing_hotkey = True
        capture_btn.config(text="キャンセル", command=cancel_capture)
        capture_label.config(text="⌨️ 設定したいキーを押してください...", fg="#0066cc")
        win.bind('<KeyPress>', on_capture_key)
        win.focus_set()

    def cancel_capture():
        global capturing_hotkey
        is_capturing[0] = False
        capturing_hotkey = False
        capture_btn.config(text="変更", command=start_capture)
        capture_label.config(text="", fg="gray")
        win.unbind('<KeyPress>')

    def on_capture_key(event):
        global capturing_hotkey
        if not is_capturing[0]:
            return
        keysym = event.keysym.lower()
        if keysym == 'escape':
            cancel_capture()
            return
        hotkey = KEYSYM_MAP.get(keysym, keysym)
        current_hotkey_var.set(hotkey)
        is_capturing[0] = False
        capturing_hotkey = False
        capture_btn.config(text="変更", command=start_capture)
        capture_label.config(text=f"✅ 「{hotkey}」に設定しました", fg="#008000")
        win.unbind('<KeyPress>')

    capture_btn.config(command=start_capture)

    # ---- 言語設定 ----
    lang_frame = tk.LabelFrame(win, text=" 認識言語 ",
                                font=("メイリオ", 10, "bold"), padx=10, pady=8)
    lang_frame.pack(fill='x', padx=15, pady=(0, 8))

    code_to_label = {v: k for k, v in LANGUAGE_OPTIONS.items()}
    current_lang_label = code_to_label.get(config.get('language', 'ja-JP'), '日本語')
    lang_var = tk.StringVar(value=current_lang_label)

    lang_combo = ttk.Combobox(lang_frame, textvariable=lang_var,
                               values=list(LANGUAGE_OPTIONS.keys()),
                               state='readonly', width=20, font=("メイリオ", 10))
    lang_combo.pack(anchor='w')

    # ---- 句読点設定 ----
    punct_frame = tk.LabelFrame(win, text=" 句読点の自動挿入 ",
                                 font=("メイリオ", 10, "bold"), padx=10, pady=8)
    punct_frame.pack(fill='x', padx=15, pady=(0, 8))

    auto_punct_var = tk.BooleanVar(value=config.get('auto_punctuation', True))
    punct_check = tk.Checkbutton(
        punct_frame, variable=auto_punct_var,
        text="句読点を自動で挿入する（。や、）",
        font=("メイリオ", 10)
    )
    punct_check.pack(anchor='w')
    tk.Label(punct_frame,
             text="例: 「今日は晴れたので散歩した」→「今日は晴れたので、散歩した。」",
             font=("メイリオ", 8), fg="gray").pack(anchor='w', pady=(2, 0))

    # ---- 保存 / キャンセル ----
    btn_frame = tk.Frame(win)
    btn_frame.pack(fill='x', padx=15, pady=(8, 15))

    def save_and_close():
        global config, capturing_hotkey
        if is_capturing[0]:
            cancel_capture()
        new_hotkey = current_hotkey_var.get().strip()
        new_language = LANGUAGE_OPTIONS.get(lang_var.get(), 'ja-JP')
        if not new_hotkey:
            capture_label.config(text="⚠️ ホットキーを設定してください", fg="red")
            return
        config['hotkey'] = new_hotkey
        config['language'] = new_language
        config['auto_punctuation'] = auto_punct_var.get()
        save_config(config)
        setup_keyboard_hook()
        logging.info(f"設定更新: hotkey={new_hotkey}, language={new_language}")
        win.destroy()

    def on_close():
        global capturing_hotkey
        if is_capturing[0]:
            capturing_hotkey = False
        win.destroy()

    tk.Button(btn_frame, text="保存して閉じる", command=save_and_close,
              font=("メイリオ", 10, "bold"), bg="#008000", fg="white",
              width=14, pady=4).pack(side='left', padx=(0, 10))
    tk.Button(btn_frame, text="キャンセル", command=on_close,
              font=("メイリオ", 10), width=10, pady=4).pack(side='left')

    win.protocol("WM_DELETE_WINDOW", on_close)

# -------------------------------------------------------
# メインループ（音声認識 + UIイベント処理）
# -------------------------------------------------------
def voice_recognition_loop():
    global is_recording_key_pressed, show_help_flag, show_settings_flag
    p = None
    stream = None

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.attributes('-alpha', 0.8)
        root.configure(bg='black')

        # ヘルプウィンドウ
        help_window = tk.Toplevel(root)
        help_window.withdraw()
        help_window.title("GreenMic の使い方")
        sw2 = root.winfo_screenwidth()
        sh2 = root.winfo_screenheight()
        help_window.geometry(f"360x310+{sw2//2 - 180}+{sh2//2 - 155}")
        help_window.attributes('-topmost', True)
        tk.Label(help_window, text="🎤 GreenMic",
                 font=("メイリオ", 14, "bold"), fg="#008000").pack(pady=10)

        def build_help_text():
            hk = config.get('hotkey', 'right ctrl')
            ll = {v: k for k, v in LANGUAGE_OPTIONS.items()}.get(
                config.get('language', 'ja-JP'), '日本語')
            punct = "オン" if config.get('auto_punctuation', True) and config.get('language', 'ja-JP').startswith('ja') else "オフ"
            return (
                f"【使い方】\n"
                f"1. 「{hk}」キーを押しっぱなしにします。\n"
                f"2. 画面下に緑の文字が出たら話します。\n"
                f"3. キーを離すと、文字が自動で入力されます。\n"
                f"4. 入力後、バーに認識テキストが約1.5秒表示されます。\n\n"
                f"認識言語: {ll}\n"
                f"句読点の自動挿入: {punct}\n"
                f"最大録音時間: {config.get('max_record_seconds', 30)}秒\n\n"
                f"設定変更はトレイアイコン → 「設定」から"
            )

        help_text_lbl = tk.Label(help_window, text=build_help_text(),
                                  font=("メイリオ", 10), justify="left")
        help_text_lbl.pack(pady=5, padx=15)
        tk.Button(help_window, text="閉じる",
                  command=help_window.withdraw, width=10).pack(pady=8)
        help_window.protocol("WM_DELETE_WINDOW", help_window.withdraw)

        # 起動通知バー
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"250x35+{sw // 2 - 125}+{sh - 150}")
        label = tk.Label(root,
                          text=f"✅ 起動しました ({config.get('hotkey', 'right ctrl')})",
                          font=("メイリオ", 10, "bold"), bg="black", fg="#00FF00")
        label.pack(expand=True)
        root.update()
        time.sleep(2)
        root.withdraw()

        CHUNK, FORMAT, CHANNELS, RATE = 1024, pyaudio.paInt16, 1, 16000
        p = pyaudio.PyAudio()
        recognizer = sr.Recognizer()
        _hide_job = [None]  # root.after() のジョブID（キャンセル用）
        logging.info(
            f"GreenMic v1.1.0 起動完了"
            f" (ホットキー: {config.get('hotkey')}, 言語: {config.get('language')})"
        )

        # ---- メインイベントループ ----
        while True:
            if show_help_flag:
                help_text_lbl.config(text=build_help_text())
                help_window.deiconify()
                show_help_flag = False

            if show_settings_flag:
                show_settings_flag = False
                open_settings_window(root)

            if not is_recording_key_pressed:
                root.update()
                time.sleep(0.02)
                continue

            # 録音開始（前回の表示タイマーをキャンセル）
            if _hide_job[0] is not None:
                root.after_cancel(_hide_job[0])
                _hide_job[0] = None
            label.config(text="🎤 録音しています...", fg="#00FF00")
            root.deiconify()
            root.update()
            logging.info("録音開始")

            frames = []
            record_start = time.time()
            max_seconds = config.get('max_record_seconds', 30)

            try:
                stream = p.open(
                    format=FORMAT, channels=CHANNELS,
                    rate=RATE, input=True,
                    frames_per_buffer=CHUNK
                )
                while is_recording_key_pressed:
                    if time.time() - record_start > max_seconds:
                        logging.warning(f"最大録音時間（{max_seconds}秒）に達したため自動停止")
                        is_recording_key_pressed = False
                        break
                    try:
                        data = stream.read(CHUNK, exception_on_overflow=False)
                        frames.append(data)
                    except OSError as e:
                        logging.error(f"録音エラー: {e}")
                        break
                    root.update()
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception as e:
                        logging.warning(f"stream close エラー: {e}")
                    stream = None

            if not frames:
                root.withdraw()
                continue

            label.config(text="⏳ 文字に変換中...", fg="#FFFF00")
            root.update()

            audio_data = sr.AudioData(b''.join(frames), RATE, p.get_sample_size(FORMAT))
            frames.clear()

            text = recognize_with_timeout(
                recognizer, audio_data,
                config.get('recognize_timeout', 10),
                config.get('language', 'ja-JP')
            )
            if text:
                # 句読点の自動挿入（設定がオンかつ日本語のとき）
                if config.get('auto_punctuation', True) and \
                   config.get('language', 'ja-JP').startswith('ja'):
                    text = add_punctuation(text)
                logging.info(f"認識結果: {text[:30]}...")
                pyperclip.copy(text)
                time.sleep(0.1)
                keyboard.send('ctrl+v')
                # 入力した文字をバーに表示（最大25文字）
                display = text if len(text) <= 25 else text[:23] + '…'
                label.config(text=f"📝 {display}", fg="#00FFFF")
                root.update()
                # 1.5秒後に非同期で消す（メインループをブロックしない）
                _hide_job[0] = root.after(1500, root.withdraw)
            else:
                root.withdraw()

            root.update()

    except Exception as e:
        logging.critical(f"致命的エラー: {e}", exc_info=True)
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if p is not None:
            try:
                p.terminate()
                logging.info("PyAudio terminate完了")
            except Exception:
                pass
        sys.exit(1)

# -------------------------------------------------------
# トレイアイコン
# -------------------------------------------------------
def trigger_help(icon, item):
    global show_help_flag
    show_help_flag = True

def trigger_settings(icon, item):
    global show_settings_flag
    show_settings_flag = True

def exit_action(icon, item):
    icon.stop()
    logging.info("GreenMic 正常終了")
    os._exit(0)

icon_path = os.path.join(BASE_PATH, 'greenmic.ico')
try:
    tray_image = Image.open(icon_path)
except Exception:
    tray_image = Image.new('RGBA', (64, 64), (255, 255, 255, 0))
    dc = ImageDraw.Draw(tray_image)
    dc.ellipse((8, 8, 56, 56), fill=(0, 200, 0))

menu = pystray.Menu(
    pystray.MenuItem('使い方を見る', trigger_help, default=True),
    pystray.MenuItem('設定',        trigger_settings),
    pystray.MenuItem('終了',        exit_action)
)

thread = threading.Thread(target=voice_recognition_loop, daemon=True)
thread.start()

icon = pystray.Icon(
    "GreenMic", tray_image,
    f"GreenMic ({config.get('hotkey', 'right ctrl')})",
    menu
)
icon.run()
