from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

@dataclass
class TeamState:
    name: str = ""
    score: int = 0
    timeouts: int = 0
    penalties: List[str] = field(default_factory=lambda: ["", "", ""])

@dataclass
class SummaryState:
    top: str = ""
    bottom: str = ""
    main: str = ""

@dataclass
class GameState:
    home: TeamState = field(default_factory=TeamState)
    away: TeamState = field(default_factory=TeamState)

    clock: str = ""
    clock_running: bool = False
    period_display: str = ""
    period_number: int = 0
    in_intermission: bool = False

    horn: bool = False
    sport: Optional[int] = None

    summary: SummaryState = field(default_factory=SummaryState)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "home": {
                "name": self.home.name,
                "score": self.home.score,
                "timeouts": self.home.timeouts,
                "penalties": self.home.penalties,
            },
            "away": {
                "name": self.away.name,
                "score": self.away.score,
                "timeouts": self.away.timeouts,
                "penalties": self.away.penalties,
            },
            "clock": self.clock,
            "clock_running": self.clock_running,
            "period_display": self.period_display,
            "period_number": self.period_number,
            "in_intermission": self.in_intermission,
            "horn": self.horn,
            "sport": self.sport,
            "summary": {
                "top": self.summary.top,
                "bottom": self.summary.bottom,
                "main": self.summary.main,
            }
        }
