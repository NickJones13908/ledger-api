"""A small SQLite-backed REST API for demonstration transactions."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import sqlite3
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator


DATABASE_PATH = os.getenv("LEDGER_DATABASE", "ledger.db")


def connect() -> sqlite3.Connection:
	connection = sqlite3.connect(DATABASE_PATH, timeout=10)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	return connection


def initialize_database() -> None:
	with connect() as connection:
		connection.execute(
			"""CREATE TABLE IF NOT EXISTS transactions (
				id TEXT PRIMARY KEY,
				idempotency_key TEXT UNIQUE,
				amount TEXT NOT NULL CHECK (CAST(amount AS REAL) > 0),
				currency TEXT NOT NULL,
				account_id TEXT NOT NULL,
				description TEXT NOT NULL,
				created_at TEXT NOT NULL
			)"""
		)
		connection.execute(
			"CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id)"
		)


@asynccontextmanager
async def lifespan(_: FastAPI):
	initialize_database()
	yield


app = FastAPI(title="Ledger API", version="1.0.0", lifespan=lifespan)


class TransactionCreate(BaseModel):
	model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

	amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
	currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
	account_id: str = Field(min_length=1, max_length=100)
	description: str = Field(min_length=1, max_length=500)

	@field_validator("amount", mode="before")
	@classmethod
	def parse_amount(cls, value):
		try:
			amount = Decimal(str(value))
		except (InvalidOperation, ValueError):
			raise ValueError("amount must be a valid decimal number") from None
		if not amount.is_finite():
			raise ValueError("amount must be finite")
		return amount


class Transaction(TransactionCreate):
	id: str
	created_at: datetime


class AccountSummary(BaseModel):
	account_id: str
	transaction_count: int
	total_value: Decimal
	currency: str | None


def serialize_transaction(row: sqlite3.Row) -> Transaction:
	return Transaction(
		id=row["id"], amount=Decimal(row["amount"]), currency=row["currency"],
		account_id=row["account_id"], description=row["description"],
		created_at=datetime.fromisoformat(row["created_at"]),
	)


@app.post("/transactions", response_model=Transaction, status_code=status.HTTP_201_CREATED)
def create_transaction(
	payload: TransactionCreate,
	response: Response,
	idempotency_key: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
):
	transaction_id = str(uuid4())
	created_at = datetime.now(timezone.utc).isoformat()
	try:
		with connect() as connection:
			connection.execute(
				"""INSERT INTO transactions
				   (id, idempotency_key, amount, currency, account_id, description, created_at)
				   VALUES (?, ?, ?, ?, ?, ?, ?)""",
				(transaction_id, idempotency_key, str(payload.amount), payload.currency,
				 payload.account_id, payload.description, created_at),
			)
			row = connection.execute(
				"SELECT * FROM transactions WHERE id = ?", (transaction_id,)
			).fetchone()
	except sqlite3.IntegrityError:
		if idempotency_key is not None:
			with connect() as connection:
				existing = connection.execute(
					"SELECT * FROM transactions WHERE idempotency_key = ?", (idempotency_key,)
				).fetchone()
			if existing:
				response.status_code = status.HTTP_200_OK
				return serialize_transaction(existing)
		raise HTTPException(status_code=409, detail="Duplicate transaction") from None
	return serialize_transaction(row)


@app.get("/transactions/{transaction_id}", response_model=Transaction)
def get_transaction(transaction_id: str):
	with connect() as connection:
		row = connection.execute(
			"SELECT * FROM transactions WHERE id = ?", (transaction_id,)
		).fetchone()
	if row is None:
		raise HTTPException(status_code=404, detail="Transaction not found")
	return serialize_transaction(row)


@app.get("/transactions", response_model=list[Transaction])
def list_transactions(
	account_id: str | None = Query(default=None, min_length=1, max_length=100),
	currency: str | None = Query(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
	limit: int = Query(default=50, ge=1, le=100),
	offset: int = Query(default=0, ge=0),
):
	clauses, parameters = [], []
	if account_id is not None:
		clauses.append("account_id = ?")
		parameters.append(account_id)
	if currency is not None:
		clauses.append("currency = ?")
		parameters.append(currency)
	where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
	with connect() as connection:
		rows = connection.execute(
			f"SELECT * FROM transactions {where} ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
			(*parameters, limit, offset),
		).fetchall()
	return [serialize_transaction(row) for row in rows]


@app.get("/accounts/{account_id}/summary", response_model=AccountSummary)
def account_summary(account_id: str):
	with connect() as connection:
		rows = connection.execute(
			"""SELECT currency, COUNT(*) AS transaction_count, SUM(CAST(amount AS NUMERIC)) AS total
			   FROM transactions WHERE account_id = ? GROUP BY currency""",
			(account_id,),
		).fetchall()
	if not rows:
		return AccountSummary(
			account_id=account_id, transaction_count=0, total_value=Decimal("0.00"), currency=None
		)
	if len(rows) > 1:
		raise HTTPException(
			status_code=409,
			detail="Account has multiple currencies; request a currency-specific summary",
		)
	row = rows[0]
	return AccountSummary(
		account_id=account_id,
		transaction_count=row["transaction_count"],
		total_value=Decimal(str(row["total"])).quantize(Decimal("0.01")),
		currency=row["currency"],
	)
