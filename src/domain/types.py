from enum import Enum, IntEnum, StrEnum


class SubjectType(IntEnum):
    """Bangumi 条目类型"""

    BOOK = 1
    ANIME = 2
    MUSIC = 3
    GAME = 4
    REAL = 6

    def to_display(self) -> str:
        """获取带 Emoji 的显示名称"""
        _map = {
            SubjectType.BOOK: "📚 书籍",
            SubjectType.ANIME: "🎬 动画",
            SubjectType.MUSIC: "🎵 音乐",
            SubjectType.GAME: "🎮 游戏",
            SubjectType.REAL: "🌐 三次元",
        }
        return _map.get(self, "未知")


class PersonType(IntEnum):
    """Bangumi 人物类型"""

    INDIVIDUAL = 1
    COMPANY = 2
    GROUP = 3

    def to_display(self) -> str:
        """获取带 Emoji 的显示名称"""
        _map = {
            PersonType.INDIVIDUAL: "👤 个人",
            PersonType.COMPANY: "🏢 公司",
            PersonType.GROUP: "👥 组合",
        }
        return _map.get(self, "未知")


class ImageSize(Enum):
    """图片尺寸规格"""

    SMALL = "small"
    GRID = "grid"
    LARGE = "large"
    MEDIUM = "medium"
    COMMON = "common"


class CommonTag(StrEnum):
    """常用标签常量"""

    TV = "TV"
    MOVIE = "剧场版"
    MANGA = "漫画"
    NOVEL = "小说"
