"""
run_summarize.py
Summarize only meaningful communities (size >= 2), then run L2/L3 rollup.

Usage:
    py run_summarize.py
    py run_summarize.py --min-size 3
"""
from __future__ import annotations
import argparse
import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.dirname(__file__))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-size", type=int, default=2,
                        help="Minimum community size to summarize (default: 2)")
    parser.add_argument("--skip-rollup", action="store_true",
                        help="Skip L2/L3 rollup after summarization")
    args = parser.parse_args()

    import chromadb
    from config.settings import settings
    from graph.igraph_community import IGraphCommunityClient
    from community.summarizer import CommunitySummarizer
    from community.global_rollup import GlobalRollup

    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)

    # Fetch community IDs with size >= min_size from SQLite
    logger.info("Fetching community IDs with size >= %d from SQLite ...", args.min_size)
    with sqlite3.connect(str(settings.sqlite_db_path)) as conn:
        rows = conn.execute(
            """
            SELECT community_id, COUNT(*) AS cnt
            FROM   nodes
            WHERE  community_id IS NOT NULL
            GROUP  BY community_id
            HAVING cnt >= ?
            ORDER  BY cnt DESC
            """,
            (args.min_size,),
        ).fetchall()
    community_ids = [r[0] for r in rows]
    logger.info("Communities to summarize: %d", len(community_ids))

    igraph_comm = IGraphCommunityClient()
    summarizer  = CommunitySummarizer(gds_client=igraph_comm, chroma_client=chroma)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    summaries = []
    failed    = 0
    with ThreadPoolExecutor(max_workers=settings.summarizer_max_workers) as executor:
        future_to_cid = {
            executor.submit(summarizer.summarize_community, cid): cid
            for cid in community_ids
        }
        for i, future in enumerate(as_completed(future_to_cid), 1):
            cid = future_to_cid[future]
            try:
                s = future.result()
                summaries.append(s)
                if i % 10 == 0 or i == len(community_ids):
                    logger.info(
                        "[%d/%d] done  community %d (%d nodes)",
                        i, len(community_ids), cid, s.node_count,
                    )
            except Exception as e:
                failed += 1
                logger.error("Failed community %d: %s", cid, e)

    logger.info("Summaries generated: %d  |  failed: %d", len(summaries), failed)
    logger.info(
        "community_summaries collection: %d items",
        chroma.get_collection("community_summaries").count(),
    )

    if args.skip_rollup:
        return

    logger.info("=== Running L2/L3 hierarchical rollup ===")
    rollup = GlobalRollup(chroma_client=chroma)
    l3 = rollup.run_full_rollup()
    logger.info("L2 domains: %d", len(l3.l2_domains))
    logger.info("L3 token count: %d", l3.token_count)
    logger.info("L3 preview: %s", l3.summary_text[:200])
    logger.info("Done.")


if __name__ == "__main__":
    main()
