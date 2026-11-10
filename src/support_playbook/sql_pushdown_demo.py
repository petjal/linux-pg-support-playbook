"""SQL query pushdown benchmark against the MVC N+1 query anti-pattern.

Demonstrates Dimitri Fontaine's 'The Art of PostgreSQL' core thesis:
pushing filtering, joining, and aggregation into the relational database engine
reduces round-trips, avoids serialization overhead, and accelerates execution.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class DepartmentSummary:
    """Aggregated department payroll metric."""

    department_id: int
    department_name: str
    active_count: int
    avg_active_salary: float
    max_active_salary: int


@dataclass(frozen=True)
class BenchmarkResult:
    """Execution telemetry comparing N+1 queries against SQL pushdown."""

    anti_pattern_query_count: int
    pushdown_query_count: int
    department_count: int
    employee_count: int
    anti_pattern_duration_ms: float
    pushdown_duration_ms: float
    speedup_factor: float
    records_match: bool
    sample_records: List[DepartmentSummary]


def setup_benchmark_database(
    department_count: int = 100,
    employees_per_department: int = 50,
) -> sqlite3.Connection:
    """Populate an in-memory SQLite relational database with deterministic test data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE departments (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY,
            department_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            salary INTEGER NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );
        """
    )

    # Populate departments
    departments_data = [
        (i, f"Department-{i:03d}", f"DEPT-{i:03d}")
        for i in range(1, department_count + 1)
    ]
    cursor.executemany(
        "INSERT INTO departments (id, name, code) VALUES (?, ?, ?);",
        departments_data,
    )

    # Populate employees
    employees_data: List[Tuple[int, int, str, int, str]] = []
    emp_id = 1
    for dept_id in range(1, department_count + 1):
        for j in range(employees_per_department):
            # Deterministic status and salary
            status = "ACTIVE" if (emp_id % 4 != 0) else "INACTIVE"
            salary = 50_000 + ((dept_id * 37 + j * 73) % 85_000)
            employees_data.append(
                (emp_id, dept_id, f"Employee-{emp_id:05d}", salary, status)
            )
            emp_id += 1

    cursor.executemany(
        "INSERT INTO employees (id, department_id, name, salary, status) VALUES (?, ?, ?, ?, ?);",
        employees_data,
    )
    cursor.execute("CREATE INDEX idx_emp_dept ON employees(department_id, status);")
    conn.commit()
    return conn


def run_anti_pattern_mvc(conn: sqlite3.Connection) -> Tuple[List[DepartmentSummary], int, float]:
    """Pattern A: MVC N+1 query and application-side looping / filtering anti-pattern.

    Executes 1 query for the parent records, then N separate queries for each child set,
    performing joins, filtering, and aggregation in Python application memory.
    """
    cursor = conn.cursor()
    query_count = 0
    t0 = time.perf_counter()

    # Query 1: Fetch parent departments
    cursor.execute("SELECT id, name FROM departments ORDER BY id;")
    query_count += 1
    dept_rows = cursor.fetchall()

    summaries: List[DepartmentSummary] = []

    # N queries: 1 query per department
    for dept_id, dept_name in dept_rows:
        cursor.execute(
            "SELECT id, department_id, name, salary, status FROM employees WHERE department_id = ?;",
            (dept_id,),
        )
        query_count += 1
        emp_rows = cursor.fetchall()

        # Application-side filtering and aggregation
        active_salaries: List[int] = []
        for _eid, _did, _ename, sal, status in emp_rows:
            if status == "ACTIVE":
                active_salaries.append(sal)

        active_count = len(active_salaries)
        avg_sal = (sum(active_salaries) / active_count) if active_count > 0 else 0.0
        max_sal = max(active_salaries) if active_count > 0 else 0

        summaries.append(
            DepartmentSummary(
                department_id=dept_id,
                department_name=dept_name,
                active_count=active_count,
                avg_active_salary=round(avg_sal, 2),
                max_active_salary=max_sal,
            )
        )

    t1 = time.perf_counter()
    duration_ms = (t1 - t0) * 1000.0
    return summaries, query_count, duration_ms


def run_pushdown_sql(conn: sqlite3.Connection) -> Tuple[List[DepartmentSummary], int, float]:
    """Pattern B: Single relational query pushing aggregation and filtering into SQL.

    Executes 1 query total, leveraging SQL JOIN, WHERE / ON filtering, and GROUP BY
    aggregation inside the database engine.
    """
    cursor = conn.cursor()
    query_count = 0
    t0 = time.perf_counter()

    sql = """
        SELECT
            d.id AS department_id,
            d.name AS department_name,
            COUNT(e.id) AS active_count,
            COALESCE(AVG(e.salary), 0.0) AS avg_active_salary,
            COALESCE(MAX(e.salary), 0) AS max_active_salary
        FROM departments d
        LEFT JOIN employees e
            ON d.id = e.department_id
            AND e.status = 'ACTIVE'
        GROUP BY d.id, d.name
        ORDER BY d.id;
    """
    cursor.execute(sql)
    query_count += 1
    rows = cursor.fetchall()

    summaries: List[DepartmentSummary] = [
        DepartmentSummary(
            department_id=row[0],
            department_name=row[1],
            active_count=row[2],
            avg_active_salary=round(float(row[3]), 2),
            max_active_salary=int(row[4]),
        )
        for row in rows
    ]

    t1 = time.perf_counter()
    duration_ms = (t1 - t0) * 1000.0
    return summaries, query_count, duration_ms


def run_sql_pushdown_benchmark(
    department_count: int = 100,
    employees_per_department: int = 50,
) -> BenchmarkResult:
    """Execute end-to-end benchmark comparing N+1 queries vs. SQL pushdown."""
    department_count = max(0, department_count)
    employees_per_department = max(0, employees_per_department)
    conn = setup_benchmark_database(department_count, employees_per_department)
    try:
        results_anti, queries_anti, duration_anti = run_anti_pattern_mvc(conn)
        results_push, queries_push, duration_push = run_pushdown_sql(conn)

        # Assert identical results
        records_match = results_anti == results_push
        speedup = duration_anti / max(duration_push, 0.001)

        total_employees = department_count * employees_per_department

        return BenchmarkResult(
            anti_pattern_query_count=queries_anti,
            pushdown_query_count=queries_push,
            department_count=department_count,
            employee_count=total_employees,
            anti_pattern_duration_ms=round(duration_anti, 3),
            pushdown_duration_ms=round(duration_push, 3),
            speedup_factor=round(speedup, 2),
            records_match=records_match,
            sample_records=results_push[:3],
        )
    finally:
        conn.close()


def main() -> None:
    """CLI runner printing benchmark findings."""
    parser = argparse.ArgumentParser(
        description="Benchmark N+1 MVC anti-pattern queries against relational SQL pushdown."
    )
    parser.add_argument("--departments", type=int, default=100, help="Number of departments")
    parser.add_argument("--employees", type=int, default=50, help="Employees per department")
    args = parser.parse_args()

    print("Running SQL Pushdown Benchmark...")
    bench = run_sql_pushdown_benchmark(args.departments, args.employees)

    print("======================================================================")
    print(" SQL PUSHDOWN vs. MVC N+1 QUERY ANTI-PATTERN BENCHMARK RESULTS")
    print("======================================================================")
    print(f"Parent Departments Audited : {bench.department_count}")
    print(f"Child Employee Records     : {bench.employee_count}")
    print(f"Dataset Verification       : {'PASS (100% Match)' if bench.records_match else 'FAIL'}")
    print("----------------------------------------------------------------------")
    print(f"Pattern A (MVC N+1 Queries): {bench.anti_pattern_query_count} queries | {bench.anti_pattern_duration_ms:.3f} ms")
    print(f"Pattern B (SQL Pushdown)   : {bench.pushdown_query_count} query   | {bench.pushdown_duration_ms:.3f} ms")
    print(f"Measured Speedup           : {bench.speedup_factor:.1f}x faster execution")
    print("----------------------------------------------------------------------")
    print("Sample Output Records (First 3):")
    for r in bench.sample_records:
        print(f"  [{r.department_name}] Active: {r.active_count} | Avg Sal: ${r.avg_active_salary:,.2f} | Max: ${r.max_active_salary:,}")
    print("======================================================================")


if __name__ == "__main__":
    main()
