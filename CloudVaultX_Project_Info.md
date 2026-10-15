# CloudVaultX

## Purpose

CloudVaultX is an AI-based decentralized cloud storage system designed
to securely store files across multiple storage nodes instead of relying
on a single centralized server.

The project aims to improve data security, availability, fault
tolerance, storage utilization, and file retrieval by intelligently
selecting storage nodes based on their performance and reliability.

## What the System Should Do

-   Allow users to securely upload and download files.
-   Authenticate users before allowing access to their files.
-   Encrypt files using AES-256 before storage.
-   Split encrypted files into fixed-size chunks.
-   Use an AI-based node selection mechanism to choose suitable storage
    nodes for each chunk.
-   Distribute file chunks across multiple storage nodes.
-   Maintain replicas of chunks so that data remains available if a
    storage node fails.
-   Monitor storage nodes using metrics such as available storage, CPU
    usage, RAM usage, network latency, uptime, and historical
    reliability.
-   Store file, chunk, node, and replication information as metadata.
-   Reassemble and decrypt file chunks when a user downloads a file.
-   Verify data integrity using checksums.
