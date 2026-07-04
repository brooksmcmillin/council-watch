import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    civicplus_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    url_date_slug: Mapped[str] = mapped_column(Text, nullable=False)
    agenda_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    minutes_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"), nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)  # "agenda" or "minutes"
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Byte size of the stored PDF, used as a cheap HEAD pre-check signal to
    # skip re-downloading unchanged source PDFs (see scraper.ensure_document).
    pdf_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    summarized_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Set when the source PDF was re-fetched and found to differ from the
    # previously stored copy. Drives a "Revised" indicator in notifications.
    revised_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    notified_email: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_bluesky: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="documents")


class ScrapeLog(Base):
    __tablename__ = "scrape_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    meetings_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_documents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revised_documents: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)
