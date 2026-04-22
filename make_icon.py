from PIL import Image, ImageDraw

# 今いる場所（同じフォルダ）に直接保存する設定
icon_path = 'greenmic.ico'

# 256x256サイズの透明な画像を作成
img = Image.new('RGBA', (256, 256), (255, 255, 255, 0))
draw = ImageDraw.Draw(img)

# マイクの土台（グレー）
draw.ellipse([88, 210, 168, 240], fill=(100, 100, 100))
draw.rectangle([118, 170, 138, 220], fill=(150, 150, 150))

# マイクのヘッド部分（鮮やかなグリーン）
draw.ellipse([88, 40, 168, 180], fill=(0, 220, 0), outline=(0, 120, 0), width=5)

# マイクの網目模様
draw.line([88, 80, 168, 80], fill=(0, 120, 0), width=4)
draw.line([88, 110, 168, 110], fill=(0, 120, 0), width=4)
draw.line([88, 140, 168, 140], fill=(0, 120, 0), width=4)

# .icoファイルとして保存！
img.save(icon_path, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (32, 32)])
print("同じフォルダ内に greenmic.ico を作成しました！")