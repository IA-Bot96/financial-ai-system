"""Cheapest possible check that the OpenAI key + model + structured-output work.

Usage:
    python -m app.engines.extraction.smoke_gpt
"""
from app.core.logging import configure_logging, get_logger
from app.engines.extraction.models.insight import InsightList
from app.engines.extraction.services.gpt_client import GPTClient

logger = get_logger("smoke")


def main() -> None:
    configure_logging(debug=True)
    gpt = GPTClient()
    logger.info("Model=%s timeout=%s retries=%s", gpt.model, gpt.settings.openai_timeout, gpt.settings.openai_max_retries)

    system = "You extract business insights. Return JSON only."
    user = (
        'Extract insights as JSON {"insights":[{area,takeaway,source_section,page,year,confidence}]} '
        "from this text:\n[page 10] CEO Review: Export volumes rose 53% on strong global demand; "
        "margins held despite cost inflation."
    )
    result = gpt.complete_structured(system, user, InsightList)
    logger.info("OK — GPT returned %d insight(s)", len(result.insights))
    for ins in result.insights:
        logger.info("  [%s] %s (conf=%s)", ins.area, ins.takeaway[:70], ins.confidence)


if __name__ == "__main__":
    main()
