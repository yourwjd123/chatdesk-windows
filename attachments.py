"""Parse only documented formats, in memory; never execute attachment content."""
import io
import mimetypes
import zipfile
from pathlib import Path

LIMIT = 20 * 1024 * 1024
TEXT_LIMIT = 200000
IMAGE_TYPES = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}


def parse_file(path, background=False):
    path = Path(path)
    if not path.is_file() or path.stat().st_size>LIMIT:
        raise ValueError('仅支持不超过20 MiB的文件。')
    raw = path.read_bytes()
    if len(raw)>LIMIT: raise ValueError('附件过大。')
    ext = path.suffix.lower()
    meta = dict(name=path.name,size=len(raw),kind='text',text='',mime='text/plain',truncated=False)
    if ext in IMAGE_TYPES:
        # Qt decodes images later; validate dimensions/signature without writing a plaintext image.
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>30000000: raise ValueError('图片超过3000万像素。')
            if image.format not in ['PNG','JPEG','WEBP']: raise ValueError('图片格式不支持。')
            mime={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}[image.format]
            image.verify()
        meta.update(kind='image',mime=mime)
        return raw,meta
    if background: raise ValueError('背景仅支持 PNG、JPEG、WebP。')
    if ext in ['.txt','.md','.csv']:
        try: text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try: text = raw.decode('gb18030')
            except UnicodeDecodeError: raise ValueError('文本编码不支持，请另存为UTF-8。') from None
    elif ext=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(raw))
        if reader.is_encrypted: raise ValueError('暂不支持加密PDF。')
        pieces=[]; length=0
        for page in reader.pages:
            t=page.extract_text() or ''; pieces.append(t); length+=len(t)
            if length>TEXT_LIMIT: break
        text='\n'.join(pieces)
        if not text.strip(): raise ValueError('PDF没有可提取文字；扫描件暂不支持OCR。')
    elif ext=='.docx':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(i.file_size for i in archive.infolist())>80*1024*1024:
                raise ValueError('文档解压后过大。')
        from docx import Document
        doc=Document(io.BytesIO(raw))
        text='\n'.join([p.text for p in doc.paragraphs]+[' | '.join(c.text for c in r.cells) for t in doc.tables for r in t.rows])
    else: raise ValueError('支持 TXT、MD、CSV、PDF、DOCX、PNG、JPEG、WebP。')
    meta.update(text=text[:TEXT_LIMIT],truncated=len(text)>TEXT_LIMIT)
    return raw,meta
