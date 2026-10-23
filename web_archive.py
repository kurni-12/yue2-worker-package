"""网站作品的本地永久归档：先保存、校验，再允许上传。"""
import hashlib
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audio_exports import export_audio, song_metadata

SHANGHAI = timezone(timedelta(hours=8))


def digest(path):
    h = hashlib.new('sha256')   # 兼容 Python 3.10（无 hashlib.file_digest）
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_component(value, limit=32):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:limit]
    return text or 'unnamed'


def archive_song(source, archive_root, job, now=None):
    source = Path(source).resolve(strict=True)
    root = Path(archive_root).resolve()
    uid, job_id = int(job.get('uid') or 0), int(job.get('id') or 0)
    username = str(job.get('username') or '').strip()
    if uid < 1 or job_id < 1 or not username:
        raise ValueError('归档缺少任务编号或真实用户信息，已停止上传')
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError('归档日期必须包含时区')
    date = now.astimezone(SHANGHAI).strftime('%Y-%m-%d')
    title = song_metadata(job)['title']
    parent = root / date / (safe_component(username) + '__' + str(uid))
    target = parent / ('%s_%s' % (job_id, safe_component(title)))
    if not target.resolve().is_relative_to(root):
        raise ValueError('归档路径超出指定目录')
    source_hash = digest(source)

    def load_verified(directory):
        manifest = json.loads((directory / 'archive.json').read_text(encoding='utf-8'))
        if (manifest['job_id'], manifest['uid'], manifest['source_sha256']) != (job_id, uid, source_hash):
            raise ValueError('已有归档与当前任务不一致，未覆盖文件')
        result = {'title': manifest['title'], 'lyrics': manifest['lyrics']}
        for fmt in ('mp3', 'flac'):
            entry = manifest['files'][fmt]
            filename = entry['filename']
            if Path(filename).name != filename:
                raise ValueError('归档文件名无效')
            file = (directory / filename).resolve(strict=True)
            if not file.is_relative_to(root) or digest(file) != entry['sha256'] or file.stat().st_size != entry['size']:
                raise ValueError('本地归档校验失败，已停止上传')
            result[fmt] = file
        result['receipt'] = {'version':1, 'job_id':job_id, 'uid':uid,
                             'files':{fmt:{'sha256':manifest['files'][fmt]['sha256'], 'size':manifest['files'][fmt]['size']} for fmt in ('mp3','flac')}}
        result['directory'] = directory
        return result

    if target.exists():
        return load_verified(target)
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.archiving_%s_' % job_id, dir=parent))
    exports = export_audio(source, staging, job)
    manifest = {'version':1, 'job_id':job_id, 'uid':uid, 'username':username,
                'title':exports['title'], 'lyrics':exports['lyrics'], 'archived_at':now.astimezone(SHANGHAI).isoformat(),
                'source_sha256':source_hash,
                'files':{fmt:{'filename':exports[fmt].name, 'sha256':digest(exports[fmt]), 'size':exports[fmt].stat().st_size} for fmt in ('mp3','flac')}}
    (staging / '歌词.txt').write_text(exports['lyrics'], encoding='utf-8')
    (staging / 'archive.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    # 同盘重命名使完整归档一次可见；失败产物保留用于恢复，不删除用户文件。
    staging.rename(target)
    return load_verified(target)
