"""SQLAlchemy models. PostgreSQL in docker-compose; SQLite works for local runs and tests."""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String(40), index=True)
    asset_class: Mapped[str] = mapped_column(String(10))
    timeframe: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(10))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal: Mapped[str | None] = mapped_column(String(60), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)          # validated alert, secret removed
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued|analyzing|analyzed|failed
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="alert", cascade="all, delete-orphan")


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    decision: Mapped[str] = mapped_column(String(24), index=True)
    market_bias: Mapped[str | None] = mapped_column(String(10), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    setup_quality: Mapped[str | None] = mapped_column(String(2), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # guarded Claude output
    claude_decision: Mapped[str | None] = mapped_column(String(24), nullable=True)
    risk: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alert: Mapped[Alert] = relationship(back_populates="analyses")


class JournalEntry(Base):
    """Paper-trade journal. Nothing here is ever sent to a broker."""
    __tablename__ = "journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(40))
    strategy: Mapped[str | None] = mapped_column(String(10), nullable=True)
    side: Mapped[str] = mapped_column(String(5))
    entry: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[list] = mapped_column(JSON, default=list)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(8), default="planned")   # planned|open|closed|skipped
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rules_followed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)   # your own yes/no per trade


class ShieldState(Base):
    """Kill switch. One row. While halted, every new alert is recorded but blocked before analysis."""
    __tablename__ = "shield_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    halted: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabRun(Base):
    """One uploaded Strategy Tester export and what Lab computed from it."""
    __tablename__ = "lab_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class ReportLog(Base):
    """Reports the coordinator sent, so each one goes out once."""
    __tablename__ = "report_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))           # daily | weekly
    day: Mapped[str] = mapped_column(String(10), index=True)  # New York date, YYYY-MM-DD
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    text: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, url: str):
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(url, **kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """create_all never alters existing tables; add new nullable columns to databases made earlier."""
        added = {"journal": {"rules_followed": "BOOLEAN"}}
        insp = inspect(self.engine)
        with self.engine.begin() as c:
            for table, cols in added.items():
                if not insp.has_table(table):
                    continue
                have = {col["name"] for col in insp.get_columns(table)}
                for name, ddl in cols.items():
                    if name not in have:
                        c.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    def ping(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.exec_driver_sql("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False
