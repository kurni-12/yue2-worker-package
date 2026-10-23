"""生成供网页使用的双格式音频；保留原文件，不访问网站。"""
import os
import re
import shutil
import subprocess
from pathlib import Path


def song_metadata(job):
    lyrics = str(job.get('lyrics') or '').strip()
    title = next((str(job[key]).strip() for key in ('title', 'song_title', 'name')
                  if job.get(key) and str(job[key]).strip()), '')
    if not title:
        title = next((line.strip() for line in lyrics.splitlines()
                      if line.strip() and not re.fullmatch(r'\[[^\]]+\]', line.strip())), '')
    title = title[:80] or 'AI歌曲 #%s' % job.get('id', '')
    return {'title': title, 'lyrics': lyrics}


def export_audio(source, destination, job, ffmpeg=None):
    """MP3 使用 320 kbps；FLAC 保留源音质，不把有损音频称为原生无损。"""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    encoder = ffmpeg or os.environ.get('YUE2_FFMPEG') or shutil.which('ffmpeg')
    if not encoder:
        raise RuntimeError('未找到 FFmpeg，请设置 YUE2_FFMPEG 或加入 PATH')
    with source.open('rb') as stream:
        native_flac = stream.read(4) == b'fLaC'
    # 不将 MP3 转成 FLAC 后标称无损原版。
    if source.suffix.lower() not in ('.flac', '.wav') or (source.suffix.lower() == '.flac' and not native_flac):
        raise ValueError('双格式导出需要真实 FLAC 或 WAV 源文件')
    metadata = song_metadata(job)
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', metadata['title']).strip(' .')[:80] or 'song'
    if filename.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}:
        filename = '_' + filename
    destination.mkdir(parents=True, exist_ok=True)
    flac, mp3 = destination / (filename + '.flac'), destination / (filename + '.mp3')
    for target in (flac, mp3):
        if target.exists() and target.resolve() != source.resolve():
            raise FileExistsError('导出文件已存在，保留原文件：%s' % target)
    if not native_flac and flac.resolve() == source.resolve():
        raise ValueError('转换目标应与源文件分开')

    def convert(target, codec):
        command = [str(encoder), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                   '-i', str(source), '-map', '0:a:0', '-vn', *codec,
                   '-metadata', 'title=' + metadata['title'], str(target)]
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=600)
        if result.returncode:
            raise RuntimeError('音频转换失败：' + result.stderr[-1500:])
    # 先转换 MP3；失败时原始文件仍保持完整。
    convert(mp3, ['-c:a', 'libmp3lame', '-b:a', '320k', '-id3v2_version', '3'])
    if flac.resolve() != source.resolve():
        if native_flac:
            shutil.copy2(source, flac)
        else:
            convert(flac, ['-c:a', 'flac'])
    return {**metadata, 'mp3': mp3, 'flac': flac,
            'mp3_mime': 'audio/mpeg', 'flac_mime': 'audio/flac'}
