const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

const dataDir = path.join(__dirname, '..', 'data');
const dbPath = path.join(dataDir, 'pams.sqlite3');
const schemaPath = path.join(__dirname, '..', 'schema.sql');

function ensureDb() {
  fs.mkdirSync(dataDir, { recursive: true });
  const db = new Database(dbPath);
  db.pragma('foreign_keys = ON');
  const schema = fs.readFileSync(schemaPath, 'utf8');
  db.exec(schema);
  return db;
}

module.exports = { ensureDb, dbPath };
