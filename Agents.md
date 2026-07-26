# Engineering & Development Standards

## 1. Development Guidelines

### Architecture & Clean Code
*   **Separation of Concerns:** Design functions and classes with a single, well-defined responsibility. Avoid monolithic functions.
*   **Object-Oriented Data Modeling:** Use strongly-typed classes, dataclasses, or Pydantic models for data representation. **Do not use raw dictionaries** for complex data structures, as they degrade maintainability and IDE autocomplete efficiency.
*   **Constants over Magic Values:** Strictly avoid magic strings and numbers. Extract all configuration keys, fixed strings, and arbitrary numbers into dedicated constants, enums, or configuration files. Make decisions when to introduce a configuration file instead of using constants so that the code doesn't become too rigid to changes


### Code Style & Documentation
*   **Strict Type Hinting:** Enforce explicit Python type hinting across all function signatures, class attributes, and variable declarations (target full compliance with static type checkers like `mypy`).
*   **Standardized Documentation:** Include PEP 257 compliant docstrings for every class, method, and function. Use PEP 8 compliant inline comments to explain complex business logic, not the code itself.

---

## 2. Testing & Guardrails

### Test Design
*   **Granular Isolation:** Logically separate test suites by specific components and functionality. Ensure every new feature or piece of logic has a corresponding unit test.
*   **Arrange-Act-Assert (AAA):** Structure all test cases using the AAA pattern to maintain clear boundaries between setup, execution, and validation.
*   **DRY Test Fixtures:** Avoid repeating global setup steps across individual tests. Centralize shared data, mocks, and configurations early using test fixtures or setup methods (`setUp`, `setUpClass`).

### Verification Pipeline
Before submitting any code changes, execute the following local verification steps:

1.  **Static Analysis:** Run `pylint` over the modified codebase and ensure there are zero failures or critical linting violations.
2.  **Test Execution:** Run `unittest` (or your preferred test runner) to ensure 100% regression-free execution and verify that no breaking changes were introduced.

### Lessons learned

exp.Expr is a real type from sqlglot, and exp.Expression type does not exist