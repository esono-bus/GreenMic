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
import logging
import concurrent.futures

# ログ設定（BUG-002,003対策: デバッグ用ログ）
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'greenmic.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)

# 二重起動の防止
mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "VoiceInputApp_Mutex")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

# --- グローバル変数 ---
is_recording_key_pressed = False
show_help_flag = False
MAX_RECORD_SECONDS = 30   # BUG-002対策: 最大録音時間
RECOGNIZE_TIMEOUT = 10    # BUG-002対策: 認識タイムアウト（秒）

# --- BUG-001対策: キーフック ---
_hook_active = False

def on_key_event(event):
    global is_recording_key_pressed
    try:
        if event.name in ('right ctrl', 'right control'):
            if event.event_type == keyboard.KEY_DOWN:
                is_recording_key_pressed = True
            elif event.event_type == keyboard.KEY_UP:
                is_recording_key_pressed = False
    except Exception as e:
        logging.warning(f"キーイベント処理エラー: {e}")

def setup_keyboard_hook():
    """BUG-001対策: キーフックの設定（管理者権限チェック付き）"""
    global _hook_active
    try:
        keyboard.hook(on_key_event)
        _hook_active = True
        logging.info("キーボードフック設定完了")
    except Exception as e:
        logging.error(f"キーボードフック設定失敗: {e}")
        _hook_active = False

def poll_right_ctrl():
    """BUG-001対策: フックが使えない場合のポーリングフォールバック"""
    global is_recording_key_pressed
    while True:
        try:
            pressed = keyboard.is_pressed('right ctrl')
            if pressed != is_recording_key_pressed:
                is_recording_key_pressed = pressed
        except Exception:
            pass
        time.sleep(0.02)

setup_keyboard_hook()

# フックが使えない場合はポーリングに切り替え
if not _hook_active:
    logging.warning("フック使用不可のためポーリングモードで起動")
    poll_thread = threading.Thread(target=poll_right_ctrl, daemon=True)
    poll_thread.start()


def recognize_with_timeout(recognizer, audio_data, timeout):
    """BUG-002対策: タイムアウト付き音声認識"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            recognizer.recognize_google, audio_data, language="ja-JP"
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


def voice_recognition_loop():
    global is_recording_key_pressed, show_help_flag
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
        help_window.geometry("320x220")
        help_window.attributes('-topmost', True)
        tk.Label(help_window, text="🎤 GreenMic", font=("メイリオ", 14, "bold"), fg="#008000").pack(pady=10)
        instructions = (
            "【使い方】\n"
            "1. 「右Ctrlキー」を押しっぱなしにします。\n"
            "2. 画面下に緑の文字が出たら話します。\n"
            "3. キーを離すと、文字が自動で入力されます。\n\n"
            f"※最大録音時間: {MAX_RECORD_SECONDS}秒"
        )
        tk.Label(help_window, text=instructions, font=("メイリオ", 10), justify="left").pack(pady=10)
        tk.Button(help_window, text="閉じる", command=help_window.withdraw, width=10).pack(pady=5)
        help_window.protocol("WM_DELETE_WINDOW", help_window.withdraw)

        # 起動通知
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        root.geometry(f"200x35+{screen_width // 2 - 100}+{screen_height - 150}")
        label = tk.Label(root, text="✅ 起動しました (右Ctrl)", font=("メイリオ", 10, "bold"), bg="black", fg="#00FF00")
        label.pack(expand=True)
        root.update()
        time.sleep(2)
        root.withdraw()

        # BUG-003対策: PyAudioを一度だけ初期化し、終了時に必ずterminateする
        CHUNK, FORMAT, CHANNELS, RATE = 1024, pyaudio.paInt16, 1, 16000
        p = pyaudio.PyAudio()
        recognizer = sr.Recognizer()
        logging.info("GreenMic v1.0.1 起動完了")

        while True:
            if show_help_flag:
                help_window.deiconify()
                show_help_flag = False

            if not is_recording_key_pressed:
                root.update()
                time.sleep(0.02)
                continue

            # 録音開始
            label.config(text="🎤 録音しています...", fg="#00FF00")
            root.deiconify()
            root.update()
            logging.info("録音開始")

            frames = []
            record_start = time.time()

            # BUG-003対策: try/finallyでstreamを確実にクローズ
            try:
                stream = p.open(
                    format=FORMAT, channels=CHANNELS,
                    rate=RATE, input=True,
                    frames_per_buffer=CHUNK
                )
                # BUG-002対策: 最大録音時間の上限
                while is_recording_key_pressed:
                    if time.time() - record_start > MAX_RECORD_SECONDS:
                        logging.warning(f"最大録音時間（{MAX_RECORD_SECONDS}秒）に達したため自動停止")
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
                # BUG-003対策: 確実にstreamをクローズ
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
            frames.clear()  # BUG-003対策: メモリ解放

            # BUG-002対策: タイムアウト付き認識
            text = recognize_with_timeout(recognizer, audio_data, RECOGNIZE_TIMEOUT)
            if text:
                logging.info(f"認識結果: {text[:30]}...")
                pyperclip.copy(text)
                time.sleep(0.1)
                keyboard.send('ctrl+v')

            root.withdraw()
            root.update()

    except Exception as e:
        logging.critical(f"致命的エラー: {e}", exc_info=True)
    finally:
        # BUG-003対策: PyAudioを必ずterminateする
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


def trigger_help(icon, item):
    global show_help_flag
    show_help_flag = True

def exit_action(icon, item):
    icon.stop()
    logging.info("GreenMic 正常終了")
    os._exit(0)

# アイコン読み込み
if getattr(sys, 'frozen', False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

icon_path = os.path.join(base_path, 'greenmic.ico')
try:
    tray_image = Image.open(icon_path)
except Exception:
    tray_image = Image.new('RGBA', (64, 64), (255, 255, 255, 0))
    dc = ImageDraw.Draw(tray_image)
    dc.ellipse((8, 8, 56, 56), fill=(0, 200, 0))

menu = pystray.Menu(
    pystray.MenuItem('使い方を見る', trigger_help, default=True),
    pystray.MenuItem('終了', exit_action)
)

thread = threading.Thread(target=voice_recognition_loop, daemon=True)
thread.start()

icon = pystray.Icon("GreenMic", tray_image, "GreenMic (右Ctrl)", menu)
icon.run()
