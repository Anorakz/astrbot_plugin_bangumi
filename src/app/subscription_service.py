from typing import TYPE_CHECKING

import aiohttp
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from ..config import ConfigManager
from ..db import BangumiRepository
from ..domain.contracts import SubscribeCandidate, SubscribeMatch, UnsubscribeMatch
from ..domain.exceptions import BangumiApiError, DatabaseError, SubscriptionError
from ..domain.schemas import Episode
from ..domain.types import ImageSize, SubjectType
from ..render import EpisodeRenderer
from .summary_translation import translate_text_to_chinese

if TYPE_CHECKING:
    from astrbot.api.star import Context

    from ..api import BangumiService


class SubscriptionService:
    def __init__(
        self,
        repository: BangumiRepository,
        service: "BangumiService",
        config_manager: ConfigManager,
        session: aiohttp.ClientSession | None = None,
        context: "Context | None" = None,
        proxy_url: str | None = None,
    ) -> None:
        self.storage = repository
        self.service = service
        self.config_manager = config_manager
        self.context = context
        self.renderer = EpisodeRenderer(
            session=session,
            render_mode=self.config_manager.get_render_mode(),
            proxy_url=proxy_url,
        )

    async def get_subscribe_candidates(
        self, keyword: str, limit: int
    ) -> tuple[str | None, list[SubscribeCandidate]]:
        """
        查询订阅候选,命中多条时由上层进行二次确认
        """
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            return "❌ 请提供要订阅的番剧关键词或ID", []

        effective_limit = max(1, min(limit, 10))
        search_res = await self.service.search_subjects(
            keyword=normalized_keyword,
            limit=effective_limit,
            subject_type=[SubjectType.ANIME.value],
            subject_tags=None,
        )
        raw_items = search_res.get("data", [])
        if not raw_items:
            return "🔍 未找到相关番剧", []

        candidates: list[SubscribeCandidate] = []
        seen: set[str] = set()
        for item in raw_items:
            subject_id_raw = item.get("id")
            if subject_id_raw is None:
                continue
            subject_id = str(subject_id_raw)
            if subject_id in seen:
                continue
            seen.add(subject_id)
            raw_name = item.get("name_cn") or item.get("name") or f"ID:{subject_id}"
            candidates.append({"subject_id": subject_id, "name": str(raw_name)})

        if not candidates:
            return "🔍 未找到相关番剧", []
        return None, candidates

    async def _build_subscribable_subject(
        self, subject_id: str
    ) -> tuple[str | None, SubscribeMatch | None]:
        """
        根据 subject_id 构建可订阅条目(详情 + 放送表校验)
        """
        details = await self.service.get_subject_details(subject_id)
        if not details:
            return "❌ 获取番剧详情失败", None

        raw_name = details.get("name_cn") or details.get("name")
        name = str(raw_name) if raw_name else "未知番剧"

        calendar_res = await self.service.get_calendar()
        is_in_calendar = False
        if calendar_res:
            for day_item in calendar_res:
                for item in day_item.get("items", []):
                    if str(item.get("id")) == subject_id:
                        is_in_calendar = True
                        break
                if is_in_calendar:
                    break

        if not is_in_calendar:
            # patched 15: 放宽订阅入口 —— 不在放送表但剧集表有播出日期的番也允许追踪
            #（未开播番会由播出计划核查走 pending 逻辑，自动预约首播窗口）
            try:
                eps_data = await self.service.get_subject_episodes(int(subject_id))
            except Exception:
                eps_data = None
            _airdays = sorted(
                {
                    str(e.get("airdate"))[:10]
                    for e in ((eps_data or {}).get("data") or [])
                    if (e.get("type") or 0) == 0 and e.get("airdate")
                }
            )
            if _airdays:
                logger.info(
                    f"[subscribe] 《{name}》不在放送表但已定档（首播 {_airdays[0]}），允许追踪"
                )
            else:
                logger.info(
                    f"[subscribe] 《{name}》不在放送表且未定档，允许追踪（每月一查）"
                )

        total_episodes_raw = details.get("eps", 0)
        total_episodes = (
            int(total_episodes_raw) if isinstance(total_episodes_raw, (int, str)) else 0
        )
        air_date = str(details.get("date", ""))
        result_data: SubscribeMatch = {
            "subject_id": subject_id,
            "name": name,
            "air_date": air_date,
            "total_episodes": total_episodes,
        }
        return None, result_data

    async def _match_subscribable_subject(
        self, keyword: str
    ) -> tuple[str | None, SubscribeMatch | None]:
        """
        查找可订阅的番剧逻辑(从 API 层迁移至此)
        """
        error_msg, candidates = await self.get_subscribe_candidates(
            keyword=keyword, limit=1
        )
        if error_msg:
            return error_msg, None
        if not candidates:
            return "🔍 未找到相关番剧", None
        return await self._build_subscribable_subject(candidates[0]["subject_id"])

    async def subscribe_by_subject_id(self, group_id: str, subject_id: str) -> str:
        """
        基于明确 subject_id 完成订阅
        """
        try:
            error_msg, subject_info = await self._build_subscribable_subject(subject_id)
            if error_msg:
                return error_msg
            if not subject_info:
                return "❌ 未知错误:未能获取番剧信息"
            return self._save_subscription(group_id, subject_info)
        except (BangumiApiError, DatabaseError, SubscriptionError) as e:
            logger.error(f"SubscriptionService.subscribe_by_subject_id 失败: {e}")
            return f"❌ 处理失败: {e}"

    async def subscribe(self, group_id: str, query: str) -> str:
        """
        处理订阅逻辑:匹配条目 -> 存入数据库 -> 建立订阅关系
        """
        logger.info(f"处理追番请求: {query}, group_id={group_id}")
        try:
            error_msg, subject_info = await self._match_subscribable_subject(query)
            if error_msg:
                return error_msg
            if not subject_info:
                return "❌ 未知错误:未能获取番剧信息"
            return self._save_subscription(group_id, subject_info)
        except (BangumiApiError, DatabaseError, SubscriptionError) as e:
            logger.error(f"SubscriptionService.subscribe 失败: {e}")
            return f"❌ 处理失败: {e}"

    def _save_subscription(self, group_id: str, subject_info: SubscribeMatch) -> str:
        success = self.storage.subscribe_subject(
            group_id=group_id,
            subject_id=subject_info["subject_id"],
            name=subject_info["name"],
            air_date=subject_info["air_date"],
            total_episodes=subject_info["total_episodes"],
        )
        if success:
            return f"✅ 成功订阅《{subject_info['name']}》!\n如有更新将推送到本群"
        return "❌ 订阅失败,数据库错误"

    async def unsubscribe(self, group_id: str, query: str) -> str:
        """
        取消订阅逻辑
        """
        logger.info(f"处理取消追番请求: {query}, group_id={group_id}")
        try:
            error_msg, subject_info = self._match_local_subscription(group_id, query)
            if error_msg:
                return error_msg
            if not subject_info:
                return "❌ 未知错误:未能获取番剧信息"

            subject_id = subject_info["subject_id"]
            name = subject_info["name"]

            success = self.storage.remove_subscription(group_id, subject_id)
            if success:
                return f"✅ 已成功取消订阅《{name}》"
            else:
                return f"❌ 取消订阅失败:你可能并没有订阅《{name}》"
        except (BangumiApiError, DatabaseError, SubscriptionError) as e:
            logger.error(f"SubscriptionService.unsubscribe 失败: {e}")
            return f"❌ 处理失败: {e}"

    def _match_local_subscription(
        self, group_id: str, query: str
    ) -> tuple[str | None, UnsubscribeMatch | None]:
        """
        在当前群组的本地订阅中做模糊匹配
        """
        normalized_query = str(query).strip()
        if not normalized_query:
            return "❌ 请提供要取消订阅的番剧关键词或ID", None

        # 取 6 条用于判断是否超过默认展示上限(5 条)
        candidates = self.storage.find_group_subscription_candidates(
            group_id=group_id, keyword=normalized_query, limit=6
        )
        if not candidates:
            return f"❌ 未找到与「{normalized_query}」匹配的本群订阅番剧", None

        if len(candidates) == 1:
            subject = candidates[0]
            return None, {
                "subject_id": str(subject.subject_id),
                "name": str(subject.name),
            }

        display_limit = 5
        display_candidates = candidates[:display_limit]
        lines = [
            "⚠️ 匹配到多个已订阅番剧,请提供更精确名称或直接使用 ID:",
        ]
        for idx, subject in enumerate(display_candidates, start=1):
            lines.append(f"{idx}. {subject.name} (ID: {subject.subject_id})")
        if len(candidates) > display_limit:
            lines.append("(仅显示前 5 项)")
        return "\n".join(lines), None

    # ── patched: 播出窗口节流排程（窗口内 15min / 窗口外 1h / 更新后 7 天）──
    def _poll_db_path(self) -> str:
        try:
            return str(self.storage.db_path)
        except Exception:
            return "/AstrBot/data/plugin_data/astrbot_plugin_bangumi_enhance/data.db"

    def _poll_state_load(self) -> dict:
        import sqlite3

        out: dict = {}
        try:
            conn = sqlite3.connect(self._poll_db_path())
            for sid, nca in conn.execute(
                "SELECT subject_id, next_check_after FROM bangumi_subjects"
            ):
                if nca:
                    out[str(sid)] = nca
            conn.close()
        except Exception as exc:
            logger.warning(f"[poll-gate] 读取排程失败: {exc}")
        return out

    def _poll_state_save(self, subject_id, next_check_after) -> None:
        import sqlite3

        try:
            conn = sqlite3.connect(self._poll_db_path())
            conn.execute(
                "UPDATE bangumi_subjects SET next_check_after = ? WHERE subject_id = ?",
                (
                    next_check_after.isoformat() if next_check_after else None,
                    str(subject_id),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[poll-gate] 写入排程失败: {exc}")

    @staticmethod
    def _parse_clock(text):
        try:
            hh, mm = str(text).strip().split(":")[:2]
            return int(hh), int(mm)
        except Exception:
            return None

    def _subject_air_weekday(self, subject_id):
        """每周播出星期：优先用显式的 air_weekday 列，缺失时从 air_date（首播日）推。"""
        import datetime as _dt
        import sqlite3 as _sq

        try:
            conn = _sq.connect(self._poll_db_path())
            row = conn.execute(
                "SELECT air_date, air_weekday FROM bangumi_subjects WHERE subject_id = ?",
                (str(subject_id),),
            ).fetchone()
            conn.close()
        except Exception:
            return None
        if row and row[1] is not None:
            try:
                return int(row[1]) % 7
            except Exception:
                pass
        raw = str((row[0] if row else "") or "").strip()
        for head in (10, 19, 26):
            try:
                return _dt.datetime.strptime(raw[:head], "%Y-%m-%d"[:head]).weekday()
            except Exception:
                continue
        return None

    def _in_air_window(self, broadcast_time, now, weekday=None) -> bool:
        """播出前 15 分钟 ～ 播出后 45 分钟（含跨午夜；weekday 给定时按星期判定）"""
        parsed = self._parse_clock(broadcast_time) if broadcast_time else None
        if not parsed:
            return False
        import datetime as _dt

        hh, mm = parsed
        for back in (0, 1):
            day = (now - _dt.timedelta(days=back)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if weekday is not None and day.weekday() != weekday:
                continue
            air = day + _dt.timedelta(hours=hh, minutes=mm)
            if air - _dt.timedelta(minutes=15) <= now <= air + _dt.timedelta(minutes=45):
                return True
        return False

    def _gate_subjects(self, subjects):
        """只保留到点的番剧（next_check_after 未到则跳过）"""
        import datetime as _dt

        now = _dt.datetime.now()
        state = self._poll_state_load()
        due = []
        for subject in subjects:
            nca = state.get(str(subject.subject_id))
            if nca:
                try:
                    if now < _dt.datetime.fromisoformat(nca):
                        continue
                except Exception:
                    pass
            due.append(subject)
        return due

    async def _advance_schedule(self, subject, notified: bool) -> None:
        import datetime as _dt

        now = _dt.datetime.now()
        clock = self._parse_clock(getattr(subject, "broadcast_time", None))

        # notified 时拉一次剧集表：用于完结判定与「无播出时刻」的预约
        _days = None
        _future = None
        if notified:
            try:
                _data = await self.service.get_subject_episodes(int(subject.subject_id))
                _all = sorted(
                    {
                        str(e.get("airdate"))[:10]
                        for e in ((_data or {}).get("data") or [])
                        if (e.get("type") or 0) == 0 and e.get("airdate")
                    }
                )
                _today = now.date().isoformat()
                _days = _all
                _future = [d for d in _all if d > _today]
            except Exception as exc:
                logger.warning(f"[plan-check] 取剧集失败 {subject.name}: {exc}")

        # 已完结退出（补丁 14：不再依赖 broadcast_time；整季放送有专属文案）
        if notified:
            try:
                import sqlite3 as _sq

                _conn2 = _sq.connect(self._poll_db_path())
                _row = _conn2.execute(
                    "SELECT current_episode, total_episodes FROM bangumi_subjects"
                    " WHERE subject_id = ?",
                    (str(subject.subject_id),),
                ).fetchone()
                _conn2.close()
            except Exception:
                _row = None
            if (
                _row
                and int(_row[1] or 0) > 0
                and int(_row[0] or 0) >= int(_row[1])
                and not _future
            ):
                if _days and len(_days) == 1:
                    msg = (
                        f"📚 《{subject.name}》今日一次性放出全 {int(_row[1])} 集"
                        f"（整季放送），已从追番列表移除。"
                    )
                else:
                    msg = (
                        f"🎬 《{subject.name}》已完结（{_row[0]}/{_row[1]}），"
                        f"已从追番列表移除。"
                    )
                await self._notice(subject, msg)
                self._remove_subject(subject.subject_id)
                logger.info(f"[plan-check] 《{subject.name}》完结，已移除追番")
                return

        if not clock:
            # 无播出时间：预约下一个播出日的中午；没有未来日 → 每天 12:00 保活
            if _future:
                nxt = _dt.datetime.fromisoformat(_future[0] + "T12:00:00")
                if nxt <= now:
                    nxt = self._next_noon(now)
            else:
                nxt = self._next_noon(now)
        elif notified:
            # 更新已捕获 → 对齐下一次播出（按播出星期，提前 15 分钟开窗）
            wd = self._subject_air_weekday(subject.subject_id)
            hh, mm = clock
            base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if wd is None:
                air = base if base > now else base + _dt.timedelta(days=1)
            else:
                air = base + _dt.timedelta(days=(wd - now.weekday()) % 7)
                if air <= now:
                    air += _dt.timedelta(days=7)
            nxt = air - _dt.timedelta(minutes=15)
            while nxt <= now:
                nxt += _dt.timedelta(days=7)
        elif self._in_air_window(
            subject.broadcast_time, now, self._subject_air_weekday(subject.subject_id)
        ):
            nxt = now + _dt.timedelta(minutes=15)  # 播出窗口内 → 15 分钟
        else:
            nxt = now + _dt.timedelta(hours=1)  # 窗口外 → 1 小时
        self._poll_state_save(subject.subject_id, nxt)


    def _next_noon(self, now):
        import datetime as _dt

        nxt = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += _dt.timedelta(days=1)
        return nxt

    def _subject_row(self, subject_id) -> dict:
        import sqlite3 as _sq

        try:
            conn = _sq.connect(self._poll_db_path())
            conn.row_factory = _sq.Row
            row = conn.execute(
                "SELECT * FROM bangumi_subjects WHERE subject_id = ?",
                (str(subject_id),),
            ).fetchone()
            conn.close()
            return dict(row) if row else {}
        except Exception:
            return {}

    def _db_set(self, subject_id, **fields) -> None:
        import sqlite3 as _sq

        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        try:
            conn = _sq.connect(self._poll_db_path())
            conn.execute(
                f"UPDATE bangumi_subjects SET {sets} WHERE subject_id = ?",
                (*fields.values(), str(subject_id)),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[plan-check] 写库失败 {fields}: {exc}")

    def _miss_inc(self, subject_id) -> int:
        import sqlite3 as _sq

        try:
            conn = _sq.connect(self._poll_db_path())
            conn.execute(
                "UPDATE bangumi_subjects SET poll_misses = COALESCE(poll_misses, 0) + 1"
                " WHERE subject_id = ?",
                (str(subject_id),),
            )
            conn.commit()
            row = conn.execute(
                "SELECT poll_misses FROM bangumi_subjects WHERE subject_id = ?",
                (str(subject_id),),
            ).fetchone()
            conn.close()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    def _miss_reset(self, subject_id) -> None:
        self._db_set(subject_id, poll_misses=0)

    def _remove_subject(self, subject_id) -> None:
        import sqlite3 as _sq

        try:
            conn = _sq.connect(self._poll_db_path())
            conn.execute("DELETE FROM bangumi_subjects WHERE subject_id = ?", (str(subject_id),))
            conn.execute("DELETE FROM subscriptions WHERE subject_id = ?", (str(subject_id),))
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[plan-check] 移除订阅失败 {subject_id}: {exc}")

    async def _notice(self, subject, text: str) -> None:
        """向该番的订阅群发一条纯文本通知（只发一次由调用方保证）"""
        from astrbot.core.message.message_event_result import MessageChain

        try:
            subs = self.storage.get_subject_subscribers(str(subject.subject_id))
        except Exception:
            subs = []
        chain = MessageChain().message(text)
        for gid in subs:
            try:
                await self._send_update_message(gid, chain)
            except Exception as exc:
                logger.warning(f"[plan-check] 通知发送失败 {gid}: {exc}")

    async def _verify_schedule(self, subject) -> bool:
        """核查播出计划；返回 True = 已改排程 / 已结案（调用方重置 miss 计数）"""
        import datetime as _dt

        today = _dt.date.today()
        try:
            data = await self.service.get_subject_episodes(int(subject.subject_id))
        except Exception as exc:
            logger.warning(f"[plan-check] 取剧集失败 {subject.name}: {exc}")
            return False
        eps = [
            e
            for e in ((data or {}).get("data") or [])
            if (e.get("type") or 0) == 0 and e.get("airdate")
        ]
        if not eps:
            # patched 16: 完全未定档（剧集表无播出日期）→ 每月一查
            _nxt = _dt.datetime.now() + _dt.timedelta(days=30)
            self._db_set(subject.subject_id, schedule_state="undated")
            self._poll_state_save(subject.subject_id, _nxt)
            logger.info(
                f"[plan-check] 《{subject.name}》未定档（剧集表无播出日期），"
                f"每月一查（下次 {_nxt:%m-%d %H:%M}）"
            )
            return True

        dated = sorted(
            (str(e.get("airdate"))[:10], e.get("sort")) for e in eps
        )
        aired = [d for d in dated if d[0] <= str(today)]
        future = [d for d in dated if d[0] > str(today)]

        row = self._subject_row(subject.subject_id)
        cur_ep = int(row.get("current_episode") or 0)
        total_ep = int(row.get("total_episodes") or 0)
        state_prev = str(row.get("schedule_state") or "")
        misses = int(row.get("poll_misses") or 0)

        # ① 只是 API 延迟：已有播出的集没抓到 → 继续等
        if aired:
            try:
                if float(str(aired[-1][1])) > cur_ep:
                    logger.info(
                        f"[plan-check] 《{subject.name}》已播 ep{aired[-1][1]} 未抓到，等待下次拉取"
                    )
                    return False
            except Exception:
                pass

        next_air = future[0] if future else None
        last_air = aired[-1][0] if aired else None
        gap = None
        if next_air and last_air:
            try:
                gap = (
                    _dt.date.fromisoformat(next_air[0]) - _dt.date.fromisoformat(last_air)
                ).days
            except Exception:
                gap = None

        # ⓪ 未开播：剧集表还没有任何已播集 → 排程对齐开播日（补丁 14）
        if not aired and next_air:
            nxt_date = next_air[0]
            clock0 = self._parse_clock(getattr(subject, "broadcast_time", None))
            base0 = _dt.datetime.fromisoformat(nxt_date + "T00:00:00")
            if clock0:
                cand0 = base0.replace(hour=clock0[0], minute=clock0[1]) - _dt.timedelta(
                    minutes=15
                )
            else:
                cand0 = base0.replace(hour=12, minute=0)
            if cand0 <= _dt.datetime.now():
                cand0 = _dt.datetime.now() + _dt.timedelta(hours=1)
            self._db_set(subject.subject_id, schedule_state="pending")
            self._poll_state_save(subject.subject_id, cand0)
            logger.info(
                f"[plan-check] 《{subject.name}》未开播（首播 {nxt_date}），"
                f"下次 {cand0:%m-%d %H:%M}"
            )
            return True

        # ④ 已完结：没有未来集且总集数已播完 → 移除追番
        if not next_air and total_ep > 0 and cur_ep >= total_ep:
            _same_day = len(dated) > 1 and len({d for d, _s in dated}) == 1
            _fin = (
                f"今日一次性放出全 {total_ep} 集（整季放送）"
                if _same_day
                else f"已完结（{cur_ep}/{total_ep}）"
            )
            await self._notice(
                subject,
                f"🎬 《{subject.name}》{_fin}，已从追番列表移除。",
            )
            self._remove_subject(subject.subject_id)
            logger.info(f"[plan-check] 《{subject.name}》完结，已移除追番")
            return True

        new_state = ""
        if next_air:
            nxt_date = next_air[0]
            try:
                nxt_wd = _dt.date.fromisoformat(nxt_date).weekday()
            except Exception:
                nxt_wd = None
            if gap is not None and gap >= 14:
                new_state = "hiatus" if gap >= 21 else "delayed"
            elif nxt_wd is not None and nxt_wd != self._subject_air_weekday(subject.subject_id):
                new_state = "delayed"
            if new_state:
                if nxt_wd is not None:
                    self._db_set(subject.subject_id, air_weekday=nxt_wd, air_date=nxt_date)
                clock = self._parse_clock(getattr(subject, "broadcast_time", None))
                nxt_dt = _dt.datetime.fromisoformat(nxt_date + "T00:00:00")
                if clock:
                    nxt_dt = nxt_dt.replace(hour=clock[0], minute=clock[1])
                nxt_check = nxt_dt - _dt.timedelta(minutes=15)
                if nxt_check <= _dt.datetime.now():
                    nxt_check = _dt.datetime.now() + _dt.timedelta(hours=1)
                self._poll_state_save(subject.subject_id, nxt_check)
                if new_state != state_prev:
                    if new_state == "hiatus":
                        msg = (
                            f"⏸ 《{subject.name}》进入休止/跨季度停播，"
                            f"预计 {nxt_date} 恢复，已按新档期排程。"
                        )
                    else:
                        msg = (
                            f"⏸ 《{subject.name}》本周停播或改档，"
                            f"下一集 {nxt_date}，已按新档期排程。"
                        )
                    await self._notice(subject, msg)
                    self._db_set(subject.subject_id, schedule_state=new_state)
                logger.info(
                    f"[plan-check] 《{subject.name}》{new_state} → 下次 {nxt_check:%m-%d %H:%M}"
                )
                return True

        # ⑥ 无异常：达上限则降级每日 12:00 保活
        if misses >= self._MAX_MISSES:
            self._poll_state_save(subject.subject_id, self._next_noon(_dt.datetime.now()))
            logger.info(
                f"[plan-check] 《{subject.name}》连续 {misses} 次未抓到，降级每日 12:00 保活"
            )
            return True
        return False

    async def _handle_miss(self, subject) -> None:
        """未抓到更新时的排程推进：窗口内 15 分钟 / 窗口外 1 小时（带上限）+ 核查"""
        import datetime as _dt

        now = _dt.datetime.now()
        wd = self._subject_air_weekday(subject.subject_id)
        if self._in_air_window(getattr(subject, "broadcast_time", None), now, wd):
            self._miss_reset(subject.subject_id)
            self._poll_state_save(subject.subject_id, now + _dt.timedelta(minutes=15))
            return
        misses = self._miss_inc(subject.subject_id)
        if misses == 1 or misses >= self._MAX_MISSES:
            try:
                if await self._verify_schedule(subject):
                    self._miss_reset(subject.subject_id)
                    return
            except Exception as exc:
                logger.warning(f"[plan-check] 核查异常 {subject.name}: {exc}")
        if misses >= self._MAX_MISSES:
            self._poll_state_save(subject.subject_id, self._next_noon(now))
        else:
            self._poll_state_save(subject.subject_id, now + _dt.timedelta(hours=1))

    # ── patched: 播出计划核查（窗口没抓到 → 查剧集 airdate 判定）──
    _MAX_MISSES = 12  # 1h 兜底轮询上限（超出降级每日 12:00 保活并重新核查）










    async def check_updates(self) -> None:
        """
        定时任务核心逻辑:检查所有监控中的番剧是否有更新

        对于设置了 broadcast_time 的番剧,仅在当前时间 >= broadcast_time
        (且 airdate 为当天)时才会触发通知,避免深夜番在凌晨就误发通知。
        """
        subjects = self._gate_subjects(self.storage.get_monitored_subjects())  # patched: 按排程节流
        logger.info(f"开始更新 {len(subjects)} 个番剧的集数信息")

        for subject in subjects:
            try:
                # 传入该番剧的广播时间,用于精确判断是否已到播出时间
                broadcast_time = getattr(subject, "broadcast_time", None)
                latest_episode = await self.service.get_latest_episode(
                    int(subject.subject_id), broadcast_time=broadcast_time
                )
                if not latest_episode:
                    await self._handle_miss(subject)  # patched: 未抓到 → 排程+核查
                    continue

                try:
                    image_base64 = await self.service.get_subject_base64image(
                        subject.subject_id, size=ImageSize.LARGE
                    )
                    if image_base64:
                        latest_episode = latest_episode.model_copy(
                            update={
                                "image_url": f"data:image/png;base64,{image_base64}"
                            }
                        )
                except BangumiApiError as e:
                    logger.error(f"获取条目 {subject.name} 图片失败: {e}")

                if latest_episode.ep > subject.current_episode:
                    logger.info(
                        f"番剧《{subject.name}》有更新: {subject.current_episode} -> {latest_episode.ep}"
                    )

                    self.storage.update_subject_episode(
                        str(subject.subject_id), latest_episode.ep
                    )

                    # patched: 静默初始化 —— 新订阅番首次抓到且已有连载进度时
                    #（本地 0 集、实际已播多集），只记进度不发通知，避免补推历史集
                    _silent = subject.current_episode == 0 and latest_episode.ep > 1
                    try:
                        import datetime as _dt2

                        _today = _dt2.datetime.now(
                            _dt2.timezone(_dt2.timedelta(hours=8))
                        ).date().isoformat()
                        if latest_episode.airdate:
                            _silent = _silent and str(latest_episode.airdate)[:10] < _today
                    except Exception:
                        pass
                    if _silent:
                        logger.info(
                            f"《{subject.name}》是新订阅番（ep{latest_episode.ep} 播于 "
                            f"{latest_episode.airdate or '?'}），静默初始化进度、不推送"
                        )
                    else:
                        await self._notify_subscribers(
                            latest_episode, str(subject.subject_id), str(subject.name)
                        )
                    await self._advance_schedule(subject, notified=True)  # patched
                else:
                    await self._handle_miss(subject)  # patched: 未抓到 → 排程+核查

            except (BangumiApiError, DatabaseError) as e:
                logger.error(f"更新番剧《{subject.name}》失败: {e}")

    async def _notify_subscribers(
        self, episode: Episode, subject_id: str, subject_name: str
    ) -> None:
        """
        渲染并发送更新通知
        """
        subscribed_groups = self.storage.get_subject_subscribers(subject_id)
        if not subscribed_groups:
            return

        episode_for_render = await self._translate_episode_summary_if_enabled(episode)

        # patched: 目标全部是官方平台(qqof:)时跳过图片——其本地分片上传
        # 不可用（upload_group_and_c2c_image 5 连败），纯文本回退已足够
        if all(str(g).startswith("qqof:") for g in subscribed_groups):
            base64_image = None
        else:
            base64_image = await self.renderer.render_episode(
                episode_for_render,
                rpc_url=self.config_manager.get_render_server_url(),
                max_retries=self.config_manager.get_max_retries(),
                variant=self.config_manager.get_episode_card_template(),
            )

        text_chain = MessageChain().message(
            f"🔔 番剧《{subject_name}》更新啦!\n第 {episode_for_render.ep} 集:{episode_for_render.name_cn or episode_for_render.name}"
        )
        chain = MessageChain()
        if base64_image:
            chain = chain.base64_image(base64_image)
        else:
            chain = text_chain

        for group_id in subscribed_groups:
            # patched: 官方平台目标只发文本（本地分片图片上传不可用，避免 2 分钟无谓重试）
            target_chain = (
                text_chain
                if (base64_image and str(group_id).startswith("qqof:"))
                else chain
            )
            try:
                await self._send_update_message(group_id, target_chain)
                logger.info(f"向群组 {group_id} 发送《{subject_name}》更新通知成功")
            except Exception as e:
                # 官方机器人平台的图片上传可能不可用,回退纯文本(rsshub 同款策略)
                logger.warning(
                    f"向群组 {group_id} 发送图片通知失败,回退纯文本: {e}"
                )
                try:
                    text_chain = MessageChain().message(
                        f"🔔 番剧《{subject_name}》更新啦!\n"
                        f"第 {episode_for_render.ep} 集:"
                        f"{episode_for_render.name_cn or episode_for_render.name}"
                    )
                    await self._send_update_message(group_id, text_chain)
                    logger.info(
                        f"向群组 {group_id} 发送《{subject_name}》更新通知成功(纯文本)"
                    )
                except Exception as e2:
                    logger.error(
                        f"向群组 {group_id} 发送《{subject_name}》更新通知失败: {e2}"
                    )

    def _platform_session_id(self) -> str:
        """取 aiocqhttp 平台实例的 id（AstrBot 4.x 里通常叫 default）。"""
        others: list[str] = []
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if getattr(meta, "name", "") == "aiocqhttp":
                    return str(meta.id)
                others.append(str(meta.id))
        except Exception:
            pass
        if len(others) == 1:
            return others[0]
        return "default"

    def _resolve_notification_session(self, group_id: str) -> str:
        """规范化成 AstrBot 4.x 要求的 <平台ID>:<MessageType>:<会话ID>。

        历史数据里可能是：
          · 纯群号                    1062494474
          · 旧格式                    aiocqhttp:group:1062494474
          · 新格式                    default:GroupMessage:1062494474
        """
        raw = str(group_id or "").strip()
        if not raw:
            return raw
        parts = raw.split(":")

        # 已是新格式
        if len(parts) == 3 and parts[1] in (
            "GroupMessage",
            "FriendMessage",
            "OtherMessage",
        ):
            return raw
        if len(parts) > 3:
            return raw  # 不认识的形态，交给 AstrBot 明确报错

        legacy = {
            "group": "GroupMessage",
            "private": "FriendMessage",
            "friend": "FriendMessage",
            "other": "OtherMessage",
        }
        if len(parts) == 3:
            kind, sid = parts[1], parts[2]
        elif len(parts) == 2:
            kind, sid = parts[0], parts[1]
        else:
            kind, sid = "group", parts[0]

        return (
            f"{self._platform_session_id()}:"
            f"{legacy.get(kind, 'GroupMessage')}:{sid}"
        )

    async def _send_update_message(
        self, group_id: str, message_chain: MessageChain
    ) -> None:
        if self.context is None:
            raise RuntimeError("AstrBot Context 不可用,无法主动发送更新通知")

        session = self._resolve_notification_session(group_id)
        if await self.context.send_message(session, message_chain):
            return

        raise RuntimeError(f"AstrBot 主动发送未找到匹配平台: {session}")

    async def _translate_episode_summary_if_enabled(self, episode: Episode) -> Episode:
        if (
            not self.config_manager.get_auto_translate_episode_summary()
            or not episode.desc.strip()
        ):
            return episode

        translated_text = await translate_text_to_chinese(
            self.context,
            episode.desc,
            feature_name="单集简介自动翻译",
        )
        if translated_text == episode.desc:
            return episode

        return episode.model_copy(update={"desc": translated_text})
