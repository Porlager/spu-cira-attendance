import pytest
import roster


def test_no_header_uses_first_two_columns():
    rows, errors = roster.parse_roster("67000001,สมชาย ใจดี\n67000002,สมหญิง รักเรียน,extra\n")
    assert rows == [("67000001", "สมชาย ใจดี"), ("67000002", "สมหญิง รักเรียน")] and errors == []


def test_header_variants():
    rows, _ = roster.parse_roster("รหัสนักศึกษา,ชื่อ-นามสกุล\n67000001,สมชาย ใจดี\n")
    assert rows == [("67000001", "สมชาย ใจดี")]
    rows, _ = roster.parse_roster("ลำดับ,รหัส,ชื่อ,นามสกุล\n1,67000001,สมชาย,ใจดี\n")
    assert rows == [("67000001", "สมชาย ใจดี")]
    rows, _ = roster.parse_roster("Student ID,First name,Last name,Email\n67000001,Somchai,Jaidee,a@b.c\n")
    assert rows == [("67000001", "Somchai Jaidee")]
    with pytest.raises(ValueError):
        roster.parse_roster("ลำดับ,อีเมล\n1,a@b.c\n")
    with pytest.raises(ValueError):
        roster.parse_roster("\n , \n")


def test_row_errors_are_reported_not_fatal():
    rows, errors = roster.parse_roster("67000001,A\nbad id!,B\n67000002,\n67000001,C\n\n67000003,D\n")
    assert rows == [("67000001", "A"), ("67000003", "D")]
    assert [e["line"] for e in errors] == [2, 3, 4]
    with pytest.raises(ValueError):
        roster.parse_roster("".join("6700%04d,x\n" % i for i in range(roster.MAX_ROWS + 1)))


def test_decode_excel_encodings():
    text = "รหัส,ชื่อ\n67000001,สมชาย\n"
    assert roster.decode_csv(("﻿" + text).encode("utf-8")) == text
    assert roster.decode_csv(text.encode("cp874")) == text
