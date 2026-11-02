"""Small synthetic page variations; no OCR or real identity data."""
import io
from PIL import Image,ImageDraw
def page_bytes(label):
    image=Image.new('RGB',(480,640),'white');ImageDraw.Draw(image).text((30,40),str(label),fill='black')
    out=io.BytesIO();image.save(out,'PNG');return out.getvalue()
