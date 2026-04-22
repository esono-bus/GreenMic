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

# 二重起動の防止
mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "VoiceInputApp_Mutex")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

# --- グローバル変数（他の機能と連携するためのスイッチ） ---
is_recording_key_pressed = False
show_help_flag = False  # 説明窓を出すかどうかのスイッチ

def on_key_event(event):
    global is_recording_key_pressed
    if event.name == 'right ctrl':
        if event.event_type == keyboard.KEY_DOWN:
            is_recording_key_pressed = True
        elif event.event_type == keyboard.KEY_UP:
            is_recording_key_pressed = False

keyboard.hook(on_key_event)

def voice_recognition_loop():
    global is_recording_key_pressed, show_help_flag
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.attributes('-alpha', 0.8)
        root.configure(bg='black')

        # --- 説明窓（ヘルプウィンドウ）の準備（最初は隠しておく） ---
        help_window = tk.Toplevel(root)
        help_window.withdraw()
        help_window.title("GreenMic の使い方")
        help_window.geometry("320x220")
        help_window.attributes('-topmost', True) # 常に最前面に表示
        
        # 説明窓の中身（テキストとボタン）
        tk.Label(help_window, text="🎤 GreenMic", font=("メイリオ", 14, "bold"), fg="#008000").pack(pady=10)
        instructions = "【使い方】\n1. 「右Ctrlキー」を押しっぱなしにします。\n2. 画面下に緑の文字が出たら話します。\n3. キーを離すと、文字が自動で入力されます。"
        tk.Label(help_window, text=instructions, font=("メイリオ", 10), justify="left").pack(pady=10)
        tk.Button(help_window, text="閉じる", command=help_window.withdraw, width=10).pack(pady=5)
        
        # 右上の「×」ボタンを押した時も隠すだけにする（エラー防止）
        help_window.protocol("WM_DELETE_WINDOW", help_window.withdraw)

        # --- 起動時のポップアップ通知 ---
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = screen_width // 2 - 100
        y = screen_height - 150
        root.geometry(f"200x35+{x}+{y}")
        
        label = tk.Label(root, text="✅ 起動しました (右Ctrl)", font=("メイリオ", 10, "bold"), bg="black", fg="#00FF00")
        label.pack(expand=True)
        
        root.update()
        time.sleep(2)
        root.withdraw()

        # 音声入力の準備
        CHUNK, FORMAT, CHANNELS, RATE = 1024, pyaudio.paInt16, 1, 16000
        p = pyaudio.PyAudio()
        recognizer = sr.Recognizer()

        while True:
            # トレイアイコンがクリックされてスイッチが入ったら、説明窓を表示！
            if show_help_flag:
                help_window.deiconify() # 隠していた窓を表示
                show_help_flag = False  # スイッチを戻す

            if not is_recording_key_pressed:
                root.update()
                time.sleep(0.02)
                continue
                
            label.config(text="🎤 録音しています...", fg="#00FF00")
            root.deiconify()
            root.update()
            
            stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
            frames = []
            while is_recording_key_pressed:
                data = stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)
                root.update()
            
            stream.stop_stream()
            stream.close()
            label.config(text="⏳ 文字に変換中...", fg="#FFFF00")
            root.update()
            
            audio_data = sr.AudioData(b''.join(frames), RATE, p.get_sample_size(FORMAT))
            try:
                text = recognizer.recognize_google(audio_data, language="ja-JP")
                pyperclip.copy(text)
                time.sleep(0.1)
                keyboard.send('ctrl+v')
            except:
                pass
                
            root.withdraw()
            root.update()

    except Exception:
        sys.exit(1)

def trigger_help(icon, item):
    """ トレイアイコンがクリックされたら、説明窓を出すスイッチをONにする """
    global show_help_flag
    show_help_flag = True

def exit_action(icon, item):
    icon.stop()
    os._exit(0)

# --- アイコン画像（.ico）を読み込む ---
# 実行ファイル（.exe）と同じ場所にあるファイルを探す
if getattr(sys, 'frozen', False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

icon_path = os.path.join(base_path, 'greenmic.ico')

try:
    # あなたが用意した greenmic.ico を読み込む！
    tray_image = Image.open(icon_path)
except Exception:
    # 万が一アイコンが見つからなかった時のための予備（緑の丸）
    tray_image = Image.new('RGBA', (64, 64), (255, 255, 255, 0))
    dc = ImageDraw.Draw(tray_image)
    dc.ellipse((8, 8, 56, 56), fill=(0, 200, 0))

# --- タスクトレイのメニュー作成 ---
# default=True を付けることで、アイコンをダブルクリックした時の基本アクションになります！
menu = pystray.Menu(
    pystray.MenuItem('使い方を見る', trigger_help, default=True),
    pystray.MenuItem('終了', exit_action)
)

# バックグラウンドで音声認識を待機させる
thread = threading.Thread(target=voice_recognition_loop, daemon=True)
thread.start()

# タスクトレイのアイコンを起動！
icon = pystray.Icon("GreenMic", tray_image, "GreenMic (右Ctrl)", menu)
icon.run()