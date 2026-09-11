-- Initialize databases for AISMR and Langfuse
-- This script runs on first PostgreSQL startup

-- Create databases
CREATE DATABASE myloware;
CREATE DATABASE langfuse;

-- Grant permissions
GRANT ALL PRIVILEGES ON DATABASE myloware TO postgres;
GRANT ALL PRIVILEGES ON DATABASE langfuse TO postgres;

-- Enable pgvector extension for vector search
\c myloware
CREATE EXTENSION IF NOT EXISTS vector;
