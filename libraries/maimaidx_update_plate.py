import hashlib
import json
import time
from io import BytesIO
from typing import Dict, List, Optional

import aiofiles
from PIL import Image, ImageDraw

from ..config import (
    levelList,
    log,
    footer_designed_generated,
    plate_tabledir,
    plate_to_dx_version,
    platecn,
    rating_table_dir,
    resolve_plate_id_list,
    TBFONT,
    version_map,
)
from .image import DrawText, draw_centered_design_footer, generate_frosted_card, music_picture
from .maimaidx_music import Music, mai
from .maimaidx_table_image import RatingGridConfig, TableImageAssets
from .maimaidx_theme import pic


_PLATE_MANIFEST_VERSION = 1
_PLATE_MANIFEST_PATH = plate_tabledir / '.manifest.json'
_RATING_MANIFEST_VERSION = 1
_RATING_MANIFEST_PATH = rating_table_dir / '.manifest.json'


def _rating_table_signature(rating: str) -> Optional[str]:
    """Return a signature for every field that affects a rating-table layout."""
    level_data = mai.total_level_data.get(rating)
    if level_data is None:
        return None

    groups = []
    for ds, songs in level_data.items():
        groups.append(
            (
                str(ds),
                [
                    (
                        int(song.id),
                        int(song.lv),
                        float(song.ds),
                        str(song.type),
                    )
                    for song in songs
                ],
            )
        )
    payload = json.dumps(
        {
            'version': _RATING_MANIFEST_VERSION,
            'rating': rating,
            'layout': {
                'start_x': RatingGridConfig.start_x,
                'start_y': RatingGridConfig.start_y,
                'gap': RatingGridConfig.gap,
                'row_count': RatingGridConfig.row_count,
            },
            'groups': groups,
        },
        ensure_ascii=False,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _load_rating_manifest() -> dict:
    try:
        data = json.loads(_RATING_MANIFEST_PATH.read_text(encoding='utf-8'))
        if data.get('version') == _RATING_MANIFEST_VERSION and isinstance(data.get('tables'), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {'version': _RATING_MANIFEST_VERSION, 'tables': {}}


def _record_rating_table_signature(rating: str) -> None:
    signature = _rating_table_signature(rating)
    if signature is None:
        return
    manifest = _load_rating_manifest()
    manifest['tables'][rating] = signature
    _RATING_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _RATING_MANIFEST_PATH.with_suffix('.json.tmp')
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    temp_path.replace(_RATING_MANIFEST_PATH)


def rating_table_is_current(rating: str) -> bool:
    signature = _rating_table_signature(rating)
    image_path = rating_table_dir / f'{rating}.png'
    if not image_path.exists() or signature is None:
        return False
    return _load_rating_manifest()['tables'].get(rating) == signature


def stale_rating_table_names() -> List[str]:
    """List rating backgrounds that are missing or no longer match live data."""
    return [rating for rating in levelList[6:] if not rating_table_is_current(rating)]


def _plate_table_signature(plate_key: str, *, is_wu: bool = False) -> Optional[str]:
    """Return a stable signature for the data that determines a plate background."""
    song_ids = resolve_plate_id_list(mai.total_plate_id_list, plate_key)
    if not song_ids:
        return None

    remaster_ids = set(mai.total_plate_id_list.get('舞ReMASTER', [])) if is_wu else set()
    rows = []
    for raw_id in song_ids:
        song_id = int(raw_id)
        song = mai.total_list.by_id(song_id)
        if song is None:
            # Keep missing catalogue entries in the signature so a later data-source
            # repair invalidates the background instead of silently blessing it.
            rows.append((song_id, None, None, None))
            continue
        index = 4 if is_wu and song_id in remaster_ids and len(song.level) > 4 else 3
        rows.append((song_id, song.level[index], float(song.ds[index]), 5 if index == 4 else 4))
    rows.sort(key=lambda row: row[0])

    payload = json.dumps(
        {'version': _PLATE_MANIFEST_VERSION, 'plate_key': plate_key, 'songs': rows},
        ensure_ascii=False,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _load_plate_manifest() -> dict:
    try:
        data = json.loads(_PLATE_MANIFEST_PATH.read_text(encoding='utf-8'))
        if data.get('version') == _PLATE_MANIFEST_VERSION and isinstance(data.get('tables'), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {'version': _PLATE_MANIFEST_VERSION, 'tables': {}}


def _record_plate_table_signature(image_key: str, plate_key: str, *, is_wu: bool = False) -> None:
    signature = _plate_table_signature(plate_key, is_wu=is_wu)
    if signature is None:
        return
    manifest = _load_plate_manifest()
    manifest['tables'][image_key] = signature
    _PLATE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _PLATE_MANIFEST_PATH.with_suffix('.json.tmp')
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    temp_path.replace(_PLATE_MANIFEST_PATH)


def plate_table_is_current(image_key: str, plate_key: str, *, is_wu: bool = False) -> bool:
    image_path = plate_tabledir / f'{image_key}.png'
    signature = _plate_table_signature(plate_key, is_wu=is_wu)
    if not image_path.exists() or signature is None:
        return False
    return _load_plate_manifest()['tables'].get(image_key) == signature


def stale_plate_table_names() -> List[str]:
    """List canonical plate backgrounds that are missing or no longer match live data."""
    stale: List[str] = []
    checked: set[str] = set()
    for raw_name in list(plate_to_dx_version.keys())[1:]:
        name = platecn.get(raw_name, raw_name)
        if name in checked:
            continue
        checked.add(name)
        _, plate_key = version_map.get(name, ([plate_to_dx_version.get(name)], name))
        if not plate_table_is_current(name, plate_key):
            stale.append(name)
    for page in (1, 2):
        image_key = f'舞-{page}'
        if not plate_table_is_current(image_key, '舞', is_wu=True):
            stale.append(image_key)
    return stale


class UpdateTable:
    def __init__(self):
        TableImageAssets.ensure_loaded()
        self.level_list = levelList[6:]
        self.version_list = list(_ for _ in plate_to_dx_version.keys())[1:]

    async def _save_image(self, im: Image.Image, path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        by = BytesIO()
        im.save(by, 'PNG')
        async with aiofiles.open(path, 'wb') as f:
            await f.write(by.getbuffer())

    def _get_level_dict(self) -> Dict[str, List[Music]]:
        return {lv: [] for lv in reversed(levelList)}

    def _get_song_list(self, version_name: str) -> List[Music]:
        song_id_list = resolve_plate_id_list(mai.total_plate_id_list, version_name)
        if not song_id_list:
            raise KeyError(f'牌子曲目列表缺失: {version_name}')
        return mai.total_list.by_id_list(song_id_list)

    async def update_level_15_rating_table(self) -> None:
        draw_time = time.time()
        assets = TableImageAssets
        lv15 = mai.total_level_data['15']['15.0']
        count = len(lv15)
        lines = (count // 3) + (1 if count % 3 else 0)
        height = 650 + lines * 450

        im = assets.generate_bg(height, 360)
        dr = ImageDraw.Draw(im)
        fot = DrawText(dr, TBFONT)
        fot.draw(495, 160, 70, 'Level.', assets.font_color, 'ld', 8, (255, 255, 255, 255))
        fot.draw(750, 160, 100, '15', assets.font_color, 'ld', 8, (255, 255, 255, 255))
        draw_centered_design_footer(
            im, fot, footer_designed_generated(),
            color=assets.font_color,
            margin_x=72,
            start_font_size=22,
            min_font_size=10,
            bottom_gap=24,
        )

        im.alpha_composite(assets.table_complete_bg, (251, 190))
        unknown_chart = Image.open(music_picture(0)).convert('RGBA').resize((330, 330))
        for i in range(lines * 3):
            row, col = divmod(i, 3)
            x = 100 + col * 425
            y = 500 + row * 450
            im.alpha_composite(assets.chart_white_bg, (x, y))
            if i < count:
                ra = lv15[i]
                chart = Image.open(music_picture(ra.id)).convert('RGBA')
                im.alpha_composite(chart.resize((330, 330)), (x + 10, y + 10))
                im.alpha_composite(assets.table_type_bg[ra.type], (x + 200, y + 345))
                full = mai.total_list.by_id(ra.id)
                if full:
                    ver_img = pic(f'{full.basic_info.version}.png')
                    if ver_img.exists():
                        im.alpha_composite(Image.open(ver_img).resize((332, 160)), (x + 9, y - 80))
                fot.draw(x + 100, y + 370, 35, ra.id, assets.font_color, 'mm')
            else:
                im.alpha_composite(unknown_chart, (x + 10, y + 10))
                im.alpha_composite(assets.table_type_bg['DX'], (x + 200, y + 345))
                fot.draw(x + 100, y + 370, 35, '????', assets.font_color, 'mm')
                fot.draw(x + 175, y + 280, 30, 'UNKNOWN', assets.font_color, 'mm', 8, (255, 255, 255, 255))

        await self._save_image(im, rating_table_dir / '15.png')
        _record_rating_table_signature('15')
        log.info(f'lv.15 定数表更新完成，耗时：{time.time() - draw_time:.3f}s')

    async def update_rating_table(self) -> str:
        assets = TableImageAssets
        rating_table_dir.mkdir(parents=True, exist_ok=True)
        all_time = 0.0
        for lv in self.level_list[:-1]:
            single_time = time.time()
            lvlist = mai.total_level_data[lv]
            grid_step = 85
            start_x = 140
            current_y = RatingGridConfig.start_y
            for songs in lvlist.values():
                current_y = RatingGridConfig.advance_group_y(current_y, len(songs))
            height = current_y + 230

            _im = assets.generate_bg(height, 360)
            im = generate_frosted_card(_im, (50, 404, 1350, current_y))
            dr = ImageDraw.Draw(im)
            tb = DrawText(dr, TBFONT)
            fot = DrawText(dr, TBFONT)
            draw_centered_design_footer(
                im, fot, footer_designed_generated(),
                color=assets.font_color,
                margin_x=72,
                start_font_size=22,
                min_font_size=10,
                bottom_gap=24,
            )

            start_y = RatingGridConfig.start_y
            for ds, songs in lvlist.items():
                if not songs:
                    continue
                sub_ds = ds.split('.')[-1]
                fot.draw(70, start_y + 35, 40, f'.{sub_ds}', assets.font_color, 'lm', 4, (255, 255, 255, 255))
                for num, music in enumerate(songs):
                    row, col = divmod(num, RatingGridConfig.row_count)
                    x = start_x + col * grid_step
                    y = start_y + row * grid_step
                    cover = Image.open(music_picture(music.id)).resize((75, 75))
                    im.alpha_composite(cover, (x, y))
                    lv_idx = int(music.lv)
                    im.alpha_composite(assets.table_diff_bg[lv_idx], (x - 5, y - 5))
                    tb.draw(
                        x + 56, y + 4, 13, music.id,
                        assets.diff_text_color[lv_idx], 'mm',
                    )
                start_y = RatingGridConfig.advance_group_y(start_y, len(songs))

            await self._save_image(im, rating_table_dir / f'{lv}.png')
            _record_rating_table_signature(lv)
            elapsed = round(time.time() - single_time, 3)
            all_time += elapsed
            log.info(f'lv.{lv} 定数表更新完成，耗时：{elapsed}s')
        return f'定数表更新完成，耗时：{all_time}s'

    def _draw_plate(
        self,
        level_dict: Dict[str, List[Music]],
        remaster_id_list: Optional[List[int]] = None,
        remaster_songs: Optional[List[Music]] = None,
        pages: Optional[int] = None,
    ) -> Image.Image:
        assets = TableImageAssets
        grid_step = 96
        start_x = 180
        current_y = 490
        for songs in level_dict.values():
            if not songs:
                continue
            rows = (len(songs) - 1) // 12 + 1
            current_y += rows * grid_step + 30
        height = current_y + 180

        _im = assets.generate_bg(height, 400)
        im = generate_frosted_card(_im, (50, 444, 1350, current_y))
        dr = ImageDraw.Draw(im)
        tb = DrawText(dr, TBFONT)
        fot = DrawText(dr, TBFONT)
        if pages is not None:
            fot.draw(700, height - 140, 40, f'Pages {pages + 1}/2', assets.font_color, 'mm')
        draw_centered_design_footer(
            im, fot, footer_designed_generated(),
            color=assets.font_color,
            margin_x=72,
            start_font_size=22,
            min_font_size=10,
            bottom_gap=24,
        )

        remaster_set = set(remaster_id_list or [])
        start_y = 490
        for ds, songs in level_dict.items():
            if not songs:
                continue

            is_wu = remaster_id_list is not None

            def _sort_key(m: Music) -> tuple[float, int]:
                if is_wu and int(m.id) in remaster_set and len(m.ds) > 4:
                    ds = m.ds[4]
                else:
                    ds = m.ds[3]
                return (-ds, int(m.id))

            songs.sort(key=_sort_key)
            fot.draw(72, start_y + 40, 40, ds, assets.font_color, 'lm', 4, (255, 255, 255, 255))
            max_row = 0
            for num, music in enumerate(songs):
                row, col = divmod(num, 12)
                max_row = max(max_row, row)
                x = start_x + col * grid_step
                y = start_y + row * grid_step
                cover = Image.open(music_picture(music.id)).resize((80, 80))
                im.alpha_composite(cover, (x, y))
                is_remaster = remaster_id_list is not None and int(music.id) in remaster_set
                id_bg = assets.table_wu_rms_id_bg if is_remaster else assets.table_id_bg
                im.alpha_composite(id_bg, (x - 5, y - 5))
                id_color = (138, 0, 226, 255) if is_remaster else (255, 255, 255, 255)
                tb.draw(x + 56, y + 4, 16, music.id, id_color, 'mm')
            start_y += (max_row + 1) * grid_step + 30
        return im

    async def update_wu_plate_table(self) -> str:
        single_time = time.time()
        song_list = self._get_song_list('舞')
        remaster_id_list = mai.total_plate_id_list['舞ReMASTER']
        remaster_songs = self._get_song_list('舞ReMASTER')
        all_level_dict = self._get_level_dict()
        for s in song_list:
            if int(s.id) in remaster_id_list and len(s.level) > 4:
                all_level_dict[s.level[4]].append(s)
            else:
                all_level_dict[s.level[3]].append(s)
        keys = list(all_level_dict.keys())
        idx = keys.index('13')
        for pages, level_dict in enumerate([
            {k: all_level_dict[k] for k in keys[:idx]},
            {k: all_level_dict[k] for k in keys[idx:]},
        ]):
            im = self._draw_plate(level_dict, remaster_id_list, remaster_songs, pages)
            image_key = f'舞-{pages + 1}'
            await self._save_image(im, plate_tabledir / f'{image_key}.png')
            _record_plate_table_signature(image_key, '舞', is_wu=True)
        log.info(f'舞/霸者完成表更新完成，耗时：{time.time() - single_time:.3f}s')
        return '舞/霸者完成表更新完成'

    async def update_plate_table(self) -> str:
        plate_tabledir.mkdir(parents=True, exist_ok=True)
        all_time = 0.0
        for name in self.version_list:
            single_time = time.time()
            if name in platecn:
                name = platecn[name]
            _, version_name = version_map.get(name, ([plate_to_dx_version.get(name)], name))
            song_list = self._get_song_list(version_name)
            level_dict = self._get_level_dict()
            for s in song_list:
                level_dict[s.level[3]].append(s)
            im = self._draw_plate(level_dict)
            await self._save_image(im, plate_tabledir / f'{name}.png')
            _record_plate_table_signature(name, version_name)
            elapsed = round(time.time() - single_time, 3)
            all_time += elapsed
            log.info(f'{name}代牌子更新完成，耗时：{elapsed}s')
        wu_result = await self.update_wu_plate_table()
        log.info(wu_result)
        return f'完成表更新完成，耗时：{all_time}s'


async def update_rating_table() -> str:
    try:
        updater = UpdateTable()
        result = await updater.update_rating_table()
        await updater.update_level_15_rating_table()
        return result
    except Exception as e:
        log.error(__import__('traceback').format_exc())
        return f'定数表更新失败，Error: {e}'


async def update_plate_table() -> str:
    try:
        updater = UpdateTable()
        return await updater.update_plate_table()
    except Exception as e:
        log.error(__import__('traceback').format_exc())
        return f'完成表更新失败，Error: {e}'
