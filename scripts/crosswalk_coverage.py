"""CIP<->SOC crosswalk coverage report.

Run as: python -m scripts.crosswalk_coverage

Reports the gaps plainly rather than hiding them: what % of 6-digit CIP codes
have at least one SOC mapping, and what % of O*NET-SOC occupations have at
least one CIP mapping.
"""

import structlog
import typer
from sqlalchemy import select

from models.cip_code import CipCode
from models.db import get_session
from models.occupation import CipSocCrosswalk, Occupation

log = structlog.get_logger()
app = typer.Typer()


@app.command()
def main() -> None:
    with get_session() as session:
        six_digit_cips = set(
            r[0]
            for r in session.execute(
                select(CipCode.cip_code).where(CipCode.level == 6)
            ).all()
        )
        all_occupations = set(
            r[0] for r in session.execute(select(Occupation.onet_soc_code)).all()
        )
        crosswalk_cips = set(
            r[0] for r in session.execute(select(CipSocCrosswalk.cip_code)).all()
        )
        crosswalk_occupations = set(
            r[0] for r in session.execute(select(CipSocCrosswalk.onet_soc_code)).all()
        )

    cip_covered = six_digit_cips & crosswalk_cips
    occ_covered = all_occupations & crosswalk_occupations

    cip_pct = 100 * len(cip_covered) / len(six_digit_cips) if six_digit_cips else 0.0
    occ_pct = (
        100 * len(occ_covered) / len(all_occupations) if all_occupations else 0.0
    )

    print("CIP <-> SOC crosswalk coverage")
    print("-------------------------------")
    print(
        f"6-digit CIP codes with >=1 SOC mapping: {len(cip_covered)}/"
        f"{len(six_digit_cips)} ({cip_pct:.1f}%)"
    )
    print(
        f"O*NET-SOC occupations with >=1 CIP mapping: {len(occ_covered)}/"
        f"{len(all_occupations)} ({occ_pct:.1f}%)"
    )
    print(
        f"6-digit CIP codes with NO SOC mapping: {len(six_digit_cips - crosswalk_cips)}"
    )
    print(
        f"O*NET-SOC occupations with NO CIP mapping: "
        f"{len(all_occupations - crosswalk_occupations)}"
    )

    log.info(
        "crosswalk_coverage.report",
        cip_covered=len(cip_covered),
        cip_total=len(six_digit_cips),
        cip_pct=round(cip_pct, 1),
        occupation_covered=len(occ_covered),
        occupation_total=len(all_occupations),
        occupation_pct=round(occ_pct, 1),
    )


if __name__ == "__main__":
    app()
