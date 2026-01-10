"""
SQL generator - creates SQLite SQL queries from natural language descriptions.
"""
import json
import re
from google.genai import Client


def generate_sql_query(
    user_request: str,
    database_schema: dict,
    current_query: str | None,
    api_key: str
) -> dict:
    """
    Generate SQL query from natural language description.

    Args:
        user_request: User's natural language request (e.g., "select all records from May 2025")
        database_schema: Database schema with tables, views, and columns
        current_query: Current SQL query in the editor (if any)
        api_key: Google API key for Gemini

    Returns:
        dict with keys:
            - success: bool
            - sql: Generated SQL query (if success)
            - error: Error message or clarifying questions (if not success)
    """
    # Create Google GenAI client
    client = Client(api_key=api_key)

    # Format schema for the prompt
    schema_description = _format_schema_for_prompt(database_schema)

    # Build the prompt
    prompt = f"""You are an expert SQL query generator for SQLite databases.

## Your Task
Generate a valid SQLite SQL query based on the user's request and the provided database schema.

## Database Schema (SQLite)
{schema_description}

## Important Rules
1. Use SQLite syntax ONLY (not MySQL, PostgreSQL, or other dialects)
2. Use double quotes for identifiers with special characters or reserved words
3. SQLite date functions: date(), datetime(), strftime()
4. SQLite does not have DATE type, dates are typically stored as TEXT in ISO format
5. For date filtering, use patterns like: strftime('%Y-%m', column) = '2025-05'
6. Always include LIMIT clause for SELECT queries to avoid returning too many rows (default: 1000)
7. Generate clean, efficient queries

## User's Request
{user_request}

{f'## Current Query (for context)' if current_query else ''}
{current_query if current_query else ''}

## Response Format
You MUST respond with a JSON object in one of these formats:

If you CAN generate the query:
{{"success": true, "sql": "YOUR SQL QUERY HERE"}}

If you CANNOT generate the query (unclear request, missing information, etc.):
{{"success": false, "error": "Explanation of what's unclear or what additional information you need"}}

Respond with ONLY the JSON object, no markdown formatting or additional text.
"""

    # Generate content
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )

    # Parse response
    response_text = response.text.strip()

    # Try to extract JSON from response
    try:
        # Remove markdown code blocks if present
        json_text = response_text
        if json_text.startswith("```"):
            # Remove opening fence
            json_text = re.sub(r"^```(?:json)?\n?", "", json_text)
            # Remove closing fence
            json_text = re.sub(r"\n?```$", "", json_text)

        result = json.loads(json_text)

        if result.get("success") and result.get("sql"):
            return {
                "success": True,
                "sql": result["sql"].strip()
            }
        elif not result.get("success") and result.get("error"):
            return {
                "success": False,
                "error": result["error"]
            }
        else:
            # Invalid response structure
            return {
                "success": False,
                "error": "Failed to generate query. Please try rephrasing your request."
            }

    except json.JSONDecodeError:
        # If response is not valid JSON, try to extract SQL directly
        # Sometimes the model returns plain SQL
        sql_match = re.search(r"SELECT[^;]+;?", response_text, re.IGNORECASE | re.DOTALL)
        if sql_match:
            return {
                "success": True,
                "sql": sql_match.group(0).strip()
            }

        return {
            "success": False,
            "error": "Failed to parse response. Please try rephrasing your request."
        }


def _format_schema_for_prompt(schema: dict) -> str:
    """Format database schema as a readable description for the LLM prompt."""
    lines = []

    # Tables
    tables = schema.get("tables", [])
    if tables:
        lines.append("### Tables")
        for table in tables:
            table_name = table.get("name", "unknown")
            columns = table.get("columns", [])
            col_descriptions = []
            for col in columns:
                col_name = col.get("name", "")
                col_type = col.get("type", "")
                is_pk = col.get("primary_key", False)
                is_nullable = col.get("nullable", True)
                pk_marker = " [PRIMARY KEY]" if is_pk else ""
                nullable_marker = "" if is_nullable else " NOT NULL"
                col_descriptions.append(f"  - {col_name}: {col_type}{pk_marker}{nullable_marker}")

            lines.append(f"\n**{table_name}**")
            lines.extend(col_descriptions)

    # Views
    views = schema.get("views", [])
    if views:
        lines.append("\n### Views")
        for view in views:
            view_name = view.get("name", "unknown")
            columns = view.get("columns", [])
            col_descriptions = []
            for col in columns:
                col_name = col.get("name", "")
                col_type = col.get("type", "")
                col_descriptions.append(f"  - {col_name}: {col_type}")

            lines.append(f"\n**{view_name}**")
            lines.extend(col_descriptions)

    return "\n".join(lines) if lines else "No schema information available"
