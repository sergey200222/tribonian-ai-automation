#!/usr/bin/env python3
"""Summarize legal documents from a local folder or a public Google Drive folder.

Text PDFs: PyMuPDF; scanned PDFs and images: OpenAI Vision.
Only source-grounded content is used in the resulting Russian summary.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.txt', '.md', '.docx'}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}
DOC_PROMPT = (
    'Ты анализируешь российские юридические документы. Работай строго с '
    'предоставленными источниками, не дополняй законами из памяти. Сохраняй '
    'номера статей, указывай страницы и исходные имена файлов. Отмечай '
    'нечитаемые места, обрезанные страницы и неполные материалы. Пиши по-русски.'
)


def path_key(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace('\\', '/')


def find_files(root: Path) -> tuple[list[Path], list[Path]]:
    files = [p for p in root.rglob('*') if p.is_file()
             and not any(part.startswith('.') for part in p.relative_to(root).parts)]
    good = sorted((p for p in files if p.suffix.lower() in SUPPORTED),
                  key=lambda p: str(p).lower())
    other = sorted((p for p in files if p.suffix.lower() not in SUPPORTED),
                   key=lambda p: str(p).lower())
    return good, other


def chunk_text(text: str, max_chars: int = 15000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    result, current = [], ''
    for paragraph in re.split(r'(?<=\n)\n', text):
        if len(current) + len(paragraph) > max_chars and current:
            result.append(current)
            current = ''
        while len(paragraph) > max_chars:
            if current:
                result.append(current)
                current = ''
            result.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        current += paragraph + '\n'
    if current.strip():
        result.append(current)
    return result


def ask_text(client: Any, model: str, instruction: str, content: str) -> str:
    response = client.responses.create(
        model=model, instructions=instruction,
        input=[{'role': 'user', 'content': [
            {'type': 'input_text', 'text': content}
        ]}], store=False
    )
    output = (response.output_text or '').strip()
    if not output:
        raise RuntimeError('Model returned an empty response')
    return output


def ask_image(client: Any, model: str, blob: bytes, mime: str, source: str) -> str:
    encoded = base64.b64encode(blob).decode('ascii')
    response = client.responses.create(
        model=model, instructions=DOC_PROMPT,
        input=[{'role': 'user', 'content': [
            {'type': 'input_text', 'text':
                f'Источник: {source}. Прочитай изображение юридического документа, '
                'извлеки существенные положения, статьи и номера пунктов. '
                'Не выдумывай неразборчивые части. Пометь обрывы текста.'},
            {'type': 'input_image', 'image_url': f'data:{mime};base64,{encoded}',
             'detail': 'high'},
        ]}], store=False
    )
    output = (response.output_text or '').strip()
    if not output:
        raise RuntimeError(f'Empty image analysis: {source}')
    return output


def image_bytes_from_pdf(page: Any) -> tuple[bytes, str]:
    import fitz
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    return pix.tobytes('png'), 'image/png'


def extract_file(path: Path, root: Path, client: Any | None,
                 model: str, check: bool = False) -> tuple[str, dict[str, Any]]:
    rel = path_key(path, root)
    ext = path.suffix.lower()
    record: dict[str, Any] = {'file': rel, 'format': ext,
                              'status': 'ok', 'pages': None}
    if ext == '.pdf':
        import fitz
        blocks = []
        with fitz.open(path) as pdf:
            record['pages'] = len(pdf)
            for n, page in enumerate(pdf, start=1):
                extracted = page.get_text('text', sort=True).strip()
                source = f'{rel}, страница PDF {n}'
                if len(re.sub(r'\s+', '', extracted)) >= 90:
                    blocks.append(f'### {source}\n{extracted}')
                elif check:
                    blocks.append(f'### {source}\n[СКАН: требуется AI Vision]')
                    record['status'] = 'needs_vision'
                else:
                    if client is None:
                        raise ValueError('OPENAI_API_KEY is required for scanned PDFs')
                    image, mime = image_bytes_from_pdf(page)
                    analysis = ask_image(client, model, image, mime, source)
                    blocks.append(f'### {source} (Vision)\n{analysis}')
        return '\n\n'.join(blocks), record
    if ext in IMAGE_EXTS:
        if check:
            record['status'] = 'needs_vision'
            return f'[{rel}: изображение, требуется Vision API]', record
        if client is None:
            raise ValueError('OPENAI_API_KEY is required for images')
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg', '.webp': 'image/webp'}[ext]
        return ask_image(client, model, path.read_bytes(), mime, rel), record
    if ext in {'.txt', '.md'}:
        return path.read_text(encoding='utf-8-sig'), record
    if ext == '.docx':
        from docx import Document
        doc = Document(path)
        lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                lines.append(' | '.join(cell.text for cell in row.cells))
        return '\n'.join(lines), record
    raise ValueError(f'Unsupported format {ext}')


def summarize_source(client: Any, model: str, rel: str, body: str) -> str:
    chunks = []
    for n, chunk in enumerate(chunk_text(body), start=1):
        chunks.append(ask_text(
            client, model, DOC_PROMPT,
            f'Файл: {rel}; фрагмент {n}\n\n{chunk}\n\n'
            'Составь фактологическую выжимку с указанием статей, источника, '
            'основных норм и ограничений. Не добавляй внешние факты.'
        ))
    if len(chunks) == 1:
        return chunks[0]
    return ask_text(client, model, DOC_PROMPT,
                    '\n\n'.join(chunks) +
                    '\n\nОбъедини в один обзор, сохрани ссылки на источники.')


def reduce_digests(client: Any, model: str, digests: list[str]) -> str:
    combined = '\n\n'.join(digests)
    while len(combined) > 48000:
        pieces = chunk_text(combined, max_chars=30000)
        if len(pieces) == 1:
            break
        compressed = [
            ask_text(client, model, DOC_PROMPT,
                     'Сократи, сохраняя факты, статьи и источники:\n' + piece)
            for piece in pieces
        ]
        next_text = '\n\n'.join(compressed)
        if len(next_text) >= len(combined):
            break
        combined = next_text
    return combined


def generate_summary(client: Any, model: str,
                     extracted: list[dict[str, str]],
                     failures: list[dict[str, str]]) -> str:
    digests = [
        f'### Источник: {item["file"]}\n' +
        summarize_source(client, model, item['file'], item['text'])
        for item in extracted
    ]
    data = reduce_digests(client, model, digests)
    missing = '\n'.join(f'- {f["file"]}: {f["error"]}' for f in failures) or 'Нет'
    return ask_text(client, model, DOC_PROMPT,
        'Подготовь единое русскоязычное саммари: 1. Общая тема; '
        '2. Основные положения по каждому документу; 3. Общие связи; '
        '4. Ограничения; 5. Использованные файлы. '
        'Не делай юридического заключения, не смешивай ГК и ГрК. '
        'Ссылайся на файлы и номера статей. '
        f'Ошибки и пропущенные источники:\n{missing}\n\nМатериалы:\n{data}')


def download_public_folder(url: str, destination: Path) -> Path:
    try:
        import gdown
    except ImportError as exc:
        raise RuntimeError('Install gdown to download Google Drive folders') from exc
    destination.mkdir(parents=True, exist_ok=True)
    downloaded = gdown.download_folder(
        url=url, output=str(destination), quiet=False, use_cookies=False)
    if not downloaded:
        raise RuntimeError('Public folder could not be downloaded. '
                           'Download it manually and use --input instead.')
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--input', type=Path)
    group.add_argument('--drive-url')
    parser.add_argument('--output', type=Path, default=Path('results'))
    parser.add_argument('--model', default='gpt-4.1-mini')
    parser.add_argument('--check', action='store_true',
                        help='No LLM calls; validate input and export manifest')
    args = parser.parse_args(argv)

    out = args.output.resolve()
    root = (download_public_folder(args.drive_url, out / 'drive_download')
            if args.drive_url else args.input.resolve())
    if not root.is_dir():
        parser.error(f'Input directory does not exist: {root}')
    if root == out or root in out.parents:
        parser.error('Output directory may not equal or contain input directory')
    out.mkdir(parents=True, exist_ok=True)
    paths, unsupported = find_files(root)
    if not paths:
        parser.error('No supported files found')

    client = None
    if not args.check:
        if not os.getenv('OPENAI_API_KEY'):
            parser.error('Set OPENAI_API_KEY or run with --check')
        from openai import OpenAI
        client = OpenAI(timeout=120.0, max_retries=2)

    records: list[dict[str, Any]] = []
    completed: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    extracts = out / 'extracted'
    extracts.mkdir(parents=True, exist_ok=True)

    for path in paths:
        rel = path_key(path, root)
        print(f'Processing: {rel}', flush=True)
        try:
            body, row = extract_file(path, root, client, args.model, args.check)
            records.append(row)
            if body.strip():
                safe_name = re.sub(r'[^\w.\-]+', '_', rel) + '.txt'
                (extracts / safe_name).write_text(body, encoding='utf-8')
                if not args.check:
                    completed.append({'file': rel, 'text': body})
        except Exception as exc:
            records.append({'file': rel, 'format': path.suffix.lower(), 'status': 'error'})
            errors.append({'file': rel, 'error': f'{type(exc).__name__}: {exc}'})
            print(f'ERROR: {rel}: {exc}', file=sys.stderr)

    for path in unsupported:
        rel = path_key(path, root)
        records.append({'file': rel, 'format': path.suffix.lower(), 'status': 'unsupported'})
        errors.append({'file': rel, 'error': 'Unsupported format'})

    manifest = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'check' if args.check else 'full',
        'source': str(root), 'model': None if args.check else args.model,
        'files': records, 'errors': errors
    }
    (out / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.check:
        print('Dry run complete. Images were not read and no summary was created.')
        return 0 if not errors else 1
    if not completed:
        print('No content extracted; cannot create summary.', file=sys.stderr)
        return 2
    try:
        result = generate_summary(client, args.model, completed, errors)
    except Exception as exc:
        print(f'Could not generate summary: {exc}', file=sys.stderr)
        return 2
    target = out / 'summary.md'
    target.write_text(result + '\n', encoding='utf-8')
    print(f'Created: {target}. Sources: {len(completed)}. Errors: {len(errors)}')
    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
