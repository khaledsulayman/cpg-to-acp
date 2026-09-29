"""Deploy extracted DMN files to the acp-writer service."""

import logging
from pathlib import Path

import click
import mlflow
import requests

from cpg_contracts import DecisionModelSummary

logger = logging.getLogger(__name__)


@mlflow.trace(name="deploy_dmn")
def deploy_dmn(
    dmn_path: Path,
    acp_writer_url: str,
    source_cpg: str | None = None,
    replace: bool = False,
) -> DecisionModelSummary:
    """POST a DMN file to the acp-writer decisions API."""
    dmn_xml = dmn_path.read_text()

    params = {}
    if source_cpg:
        params["source_cpg"] = source_cpg
    if replace:
        params["replace"] = "true"

    r = requests.post(
        f"{acp_writer_url}/api/v1/decisions/models",
        data=dmn_xml,
        headers={"Content-Type": "application/xml"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()

    summary = DecisionModelSummary.model_validate(r.json())
    logger.info(
        "Deployed %s (%s): %d inputs, %d outputs",
        summary.name, summary.id, len(summary.inputs), len(summary.outputs),
    )
    return summary


@click.command()
@click.argument("dmn_files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--acp-writer-url", default="http://localhost:8082", help="ACP Writer base URL.")
@click.option("--source-cpg", default=None, help="CPG ID to link deployed models to.")
@click.option("--replace", is_flag=True, help="Replace a colliding model from another source CPG.")
def main(dmn_files: tuple[Path, ...], acp_writer_url: str, source_cpg: str | None, replace: bool):
    """Deploy DMN files to the acp-writer service."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not dmn_files:
        click.echo("No DMN files specified.")
        return

    for dmn_path in dmn_files:
        try:
            summary = deploy_dmn(dmn_path, acp_writer_url, source_cpg=source_cpg, replace=replace)
            click.echo(f"  Deployed: {summary.name} ({summary.id})")
        except requests.HTTPError as e:
            click.echo(f"  FAILED: {dmn_path.name} — {e}", err=True)
        except requests.ConnectionError:
            click.echo(f"  FAILED: {dmn_path.name} — cannot connect to {acp_writer_url}", err=True)


if __name__ == "__main__":
    main()
