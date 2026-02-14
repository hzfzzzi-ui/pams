const Database=require('better-sqlite3');
const db=new Database('C:/Users/hzfzzzi/openclaw/pams/data/pams.sqlite3');
const rows=db.prepare("select p.symbol,p.marketValue,p.symbolType,a.name as accountName,a.kind as kind,a.currency as ccy from positions p join accounts a on a.id=p.accountId where p.asOfDate=? and a.kind='liability'").all('2026-02-13');
console.log('liab count',rows.length);
console.log(rows);
