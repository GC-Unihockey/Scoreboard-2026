from dataclasses import dataclass
from state import GameState

def _clock_is_not_zero(clock: str) -> bool:
    c = clock.strip()
    return c not in ("0:00", "00:00", "")

@dataclass
class SummaryCalculator:
    active_period: str = "1"
    nV: str = ""

    home_1: int = 0
    away_1: int = 0
    home_2: int = 0
    away_2: int = 0
    home_3: int = 0
    away_3: int = 0
    home_o: int = 0
    away_o: int = 0

    last_top: str = ""
    last_bottom: str = ""
    last_main: str = ""

    def update(self, state: GameState) -> bool:
        period = (state.period_display or "").strip() or self.active_period
        if period != self.active_period and _clock_is_not_zero(state.clock):
            self.active_period = period
            self.nV = ""

        hs = state.home.score
        as_ = state.away.score

        pauseninfo = ""
        drittels = ""
        zwischen = f"{hs}:{as_}"

        if self.active_period == "1":
            pauseninfo = "RESULTAT 1. DRITTEL"
            self.home_1, self.away_1 = hs, as_
            drittels = f"({self.home_1}:{self.away_1})"

        elif self.active_period == "2":
            pauseninfo = "RESULTAT 2. DRITTEL"
            self.home_2 = hs - self.home_1
            self.away_2 = as_ - self.away_1
            drittels = f"({self.home_1}:{self.away_1}, {self.home_2}:{self.away_2})"

        elif self.active_period == "3":
            pauseninfo = "RESULTAT 3. DRITTEL"
            self.home_3 = hs - self.home_2 - self.home_1
            self.away_3 = as_ - self.away_2 - self.away_1
            if hs != as_:
                pauseninfo = "SCHLUSSRESULTAT"
            drittels = f"({self.home_1}:{self.away_1}, {self.home_2}:{self.away_2}, {self.home_3}:{self.away_3})"

        elif self.active_period == "O":
            self.home_o = hs - self.home_3 - self.home_2 - self.home_1
            self.away_o = as_ - self.away_3 - self.away_2 - self.away_1
            drittels = f"({self.home_1}:{self.away_1}, {self.home_2}:{self.away_2}, {self.home_3}:{self.away_3}, {self.home_o}:{self.away_o})"
            if hs == as_:
                pauseninfo = "RESULTAT NACH VERLAENGERUNG"
                self.nV = ""
            else:
                pauseninfo = "SCHLUSSRESULTAT"
                self.nV = " (n.V.)"

        top = pauseninfo
        bottom = drittels
        main = zwischen + self.nV

        changed = (top != self.last_top) or (bottom != self.last_bottom) or (main != self.last_main)
        if changed:
            state.summary.top = top
            state.summary.bottom = bottom
            state.summary.main = main
            self.last_top, self.last_bottom, self.last_main = top, bottom, main
        return changed
