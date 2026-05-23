#!/usr/bin/env node
import { ensureDatabaseAndSchemaFromEnv, mysqlConfigFromEnv } from '../db.mjs';

const { database } = mysqlConfigFromEnv(process.env, { includeDatabase: false });

try {
  await ensureDatabaseAndSchemaFromEnv();
  console.error(`MySQL schema is ready: ${database}`);
} catch (error) {
  console.error(`FATAL db:migrate failed: ${error.message}`);
  process.exit(1);
}
