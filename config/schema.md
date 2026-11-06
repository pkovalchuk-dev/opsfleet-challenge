# Data available to the assistant

Dataset: `bigquery-public-data.thelook_ecommerce`, a fictitious online clothing
retailer. Refer to tables by their bare names below; the application scopes
them to the user's brands automatically. Data runs from January 2019 to the
current date. There is no currency column; treat prices as plain amounts.

## Tables

### products — one row per catalog product
| column | type | meaning |
| --- | --- | --- |
| id | INT64 | product id (joins order_items.product_id) |
| name | STRING | product name, e.g. "Calvin Klein Women's MSY Crew Neck Roll Sleeve" |
| brand | STRING | brand, e.g. "Calvin Klein", "Seven7", "Bailey 44" |
| category | STRING | one of: Intimates, Jeans, Tops & Tees, Fashion Hoodies & Sweatshirts, Swim, Sleep & Lounge, Shorts, Sweaters, Accessories, Active, Outerwear & Coats, Underwear, Pants, Dresses, Socks, Maternity, Plus, Suits & Sport Coats, Socks & Hosiery, Pants & Capris, Leggings, Blazers & Jackets, Skirts, Suits, Jumpsuits & Rompers, Clothing Sets |
| department | STRING | "Women" or "Men" |
| retail_price | FLOAT64 | catalog list price |
| cost | FLOAT64 | unit cost to the retailer (margin = sale_price - cost) |
| sku | STRING | stock keeping unit |
| distribution_center_id | INT64 | warehouse id |

### order_items — one row per unit sold (the sales fact table)
| column | type | meaning |
| --- | --- | --- |
| id | INT64 | line id |
| order_id | INT64 | joins orders.order_id |
| user_id | INT64 | the shopper, same as orders.user_id |
| product_id | INT64 | joins products.id |
| inventory_item_id | INT64 | stock unit |
| status | STRING | one of: Complete, Shipped, Processing, Cancelled, Returned |
| created_at | TIMESTAMP | when the line was ordered |
| shipped_at, delivered_at, returned_at | TIMESTAMP | lifecycle timestamps, NULL until they happen |
| sale_price | FLOAT64 | price paid for this one unit |

### orders — one row per order
| column | type | meaning |
| --- | --- | --- |
| order_id | INT64 | order id |
| user_id | INT64 | the shopper (joins users.id) |
| status | STRING | same values as order_items.status |
| gender | STRING | shopper gender on the order: "M" or "F" |
| created_at | TIMESTAMP | when the order was placed |
| shipped_at, delivered_at, returned_at | TIMESTAMP | lifecycle timestamps |
| num_of_item | INT64 | items in this order within the user's brands (all items for the CEO). Count scoped order_items for period- or status-specific totals. |

### users — one row per shopper (not the executives using this assistant)
| column | type | meaning |
| --- | --- | --- |
| id | INT64 | shopper id; always refer to shoppers by this id |
| age | INT64 | age in years |
| gender | STRING | "M" or "F" |
| state | STRING | state or region name, e.g. "California", "Texas", "Guangdong" |
| city | STRING | city name |
| country | STRING | e.g. United States, China, Brasil, South Korea, France, United Kingdom, Germany, Spain, Japan, Australia |
| traffic_source | STRING | acquisition channel: Search, Organic, Facebook, Email, Display |
| created_at | TIMESTAMP | sign-up date |

The source database contains personal columns such as names, email, street address,
postal code and coordinates. The assistant's query policy blocks those columns.
They must never be requested or displayed. Do not claim they are absent from, or
never stored in, the source database.

## Joins
- order_items.product_id = products.id
- order_items.order_id = orders.order_id
- order_items.user_id = users.id (same as orders.user_id = users.id)

## Metric definitions
- **Completed sales value** (the default for sales or spend): `SUM(order_items.sale_price)` where
  `order_items.status = 'Complete'`, `delivered_at IS NOT NULL` and `delivered_at <= @analysis_cutoff`,
  filtered by `DATE(orders.created_at)` after joining on `order_id`. This is not accounting revenue.
  Use all statuses only if the user explicitly asks for gross or placed orders.
- **Orders**: `COUNT(DISTINCT order_items.order_id)`.
- **Items sold**: `COUNT(*)` on order_items with the same completed-sales conditions.
- **Customers**: `COUNT(DISTINCT order_items.user_id)`.
- **Top customers**: group completed spend by `order_items.user_id`, include distinct order count,
  and rank by spend descending (then user_id for ties). Default to 10 customers unless a count is given.
  Customer rankings and spend are allowed; show shopper IDs, never names or contact details.
- **Average order value**: sales divided by distinct orders.
- **Returns**: items with `status = 'Returned'` and `returned_at <= @analysis_cutoff`; return rate = returned items
  divided by completed plus returned items, each with its corresponding cutoff condition.
- **Margin**: `SUM(sale_price - products.cost)` with the same completed-sales conditions.
- **Churn** has no standard definition in this data. State the assumption you
  use, for example customers with a completed order before the period and none
  during it.

## Date conventions
- Sales periods use `DATE(orders.created_at)`. Join orders to order_items on order_id.
  Item creation dates can differ from order dates and must not substitute for them.
- Python supplies one `@analysis_cutoff` TIMESTAMP per analysis. Set the proposal's
  `analysis_cutoff` to the requested timestamp, or null for the current-time default.
  Use that parameter for all cutoff bounds and state its exact timestamp in the answer.
  Lifecycle timestamps can be in the future: exclude deliveries and returns after the cutoff.
  Current status is not a historical status ledger, so a cutoff cannot reconstruct exact past accounting balances.
- Quarters: Q1 = Jan–Mar, Q2 = Apr–Jun, Q3 = Jul–Sep, Q4 = Oct–Dec. If the
  user gives a quarter without a year, use the current year.
- "Last month", "this year" and similar are relative to `DATE(@analysis_cutoff)`.
- For customer rankings without a period in the question or conversation, use all available dates
  through the analysis cutoff and state that window.
- Year-to-date and other windows ending at the cutoff must also require
  `orders.created_at <= @analysis_cutoff`, not just a date comparison.
- Monthly series: `DATE_TRUNC(DATE(orders.created_at), MONTH) AS month`.
- To include months with no completed sales, use one conditional aggregate per requested
  month, anchored on products with a LEFT JOIN to a nested SELECT of qualifying items
  joined to orders. Apply order-date, status and delivery filters inside that SELECT
  so products with no matching sales remain. A wide
  table with month columns is valid. Never generate a calendar with WITH, UNION or UNNEST.
  Zero means no matching completed sales for an existing scoped product or brand, not
  missing data or an unavailable query. A percentage change from zero is N/A, not 0%.

## SQL rules
- One SELECT statement in BigQuery Standard SQL. No CTEs, no UNION, no
  comments, no DDL or DML.
- Use the bare table names orders, order_items, products, users with aliases.
- Never select a personal column and never use `SELECT *`.
- Always `GROUP BY` the non-aggregated columns and add an `ORDER BY`.
- Add a SQL `LIMIT` only when the user requests a bounded result, such as the top 10.
  The backend fetches all result rows in pages of up to 200, following every page token. Model prompts show bounded samples. Calculate totals and comparisons in SQL.
- Identify customers by `user_id` only.
- Nested SELECTs in FROM or WHERE are allowed.
- The only permitted query parameter is the Python-bound `@analysis_cutoff`.

## Examples
Question: Sales for each of my brands in July, August and September 2026, including zero
months, with August-to-September change. Cutoff: 2026-10-01 00:00:00 UTC.
```sql
SELECT m.brand, m.items_sold, ROUND(m.july_sales, 2) AS july_sales,
       ROUND(m.august_sales, 2) AS august_sales,
       ROUND(m.september_sales, 2) AS september_sales,
       ROUND(m.september_sales - m.august_sales, 2) AS sales_change,
       ROUND(100 * SAFE_DIVIDE(m.september_sales - m.august_sales, m.august_sales), 2) AS sales_change_pct
FROM (
  SELECT p.brand, COUNT(s.id) AS items_sold,
         SUM(IF(DATE(s.ordered_at) < '2026-08-01', s.sale_price, 0)) AS july_sales,
         SUM(IF(DATE(s.ordered_at) >= '2026-08-01' AND DATE(s.ordered_at) < '2026-09-01', s.sale_price, 0)) AS august_sales,
         SUM(IF(DATE(s.ordered_at) >= '2026-09-01', s.sale_price, 0)) AS september_sales
  FROM products p LEFT JOIN (
    SELECT oi.id, oi.product_id, oi.sale_price, o.created_at AS ordered_at
    FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
    WHERE oi.status = 'Complete' AND oi.delivered_at IS NOT NULL
      AND oi.delivered_at <= @analysis_cutoff
      AND DATE(o.created_at) >= '2026-07-01' AND DATE(o.created_at) < '2026-10-01'
  ) s ON s.product_id = p.id
  GROUP BY p.brand
) m
ORDER BY m.brand
```
The product join counts each item once. Add `COUNTIF` per month if units are requested,
not `COUNT(*)`, which would count an unmatched product as a sale. Interpret a NULL
percentage as N/A when the previous month is zero. Do not infer zeros for brands absent
from the scoped catalog or for results outside the prompt sample.

Question: Monthly sales by brand for the last 6 months.
```sql
SELECT p.brand, DATE_TRUNC(DATE(o.created_at), MONTH) AS month,
       ROUND(SUM(oi.sale_price), 2) AS sales, COUNT(DISTINCT oi.order_id) AS orders
FROM order_items oi JOIN products p ON p.id = oi.product_id
JOIN orders o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete'
  AND oi.delivered_at IS NOT NULL AND oi.delivered_at <= @analysis_cutoff
  AND DATE(o.created_at) >= DATE_SUB(DATE_TRUNC(DATE(@analysis_cutoff), MONTH), INTERVAL 5 MONTH)
  AND o.created_at <= @analysis_cutoff
GROUP BY p.brand, month ORDER BY month, p.brand
```

Question: Top 10 customers by completed spend this year.
```sql
SELECT oi.user_id, ROUND(SUM(oi.sale_price), 2) AS spend,
       COUNT(DISTINCT oi.order_id) AS orders
FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete' AND EXTRACT(YEAR FROM o.created_at) = EXTRACT(YEAR FROM DATE(@analysis_cutoff))
  AND o.created_at <= @analysis_cutoff
  AND oi.delivered_at IS NOT NULL AND oi.delivered_at <= @analysis_cutoff
GROUP BY oi.user_id ORDER BY spend DESC, oi.user_id LIMIT 10
```

Question: Compare two products by sales, items and return rate.
```sql
SELECT p.name,
       ROUND(SUM(IF(oi.status = 'Complete', oi.sale_price, 0)), 2) AS sales,
       COUNTIF(oi.status = 'Complete') AS items_sold,
       ROUND(SAFE_DIVIDE(COUNTIF(oi.status = 'Returned'),
             COUNTIF(oi.status IN ('Complete', 'Returned'))), 3) AS return_rate
FROM order_items oi JOIN products p ON p.id = oi.product_id
WHERE p.name IN ('Product A', 'Product B')
  AND ((oi.status = 'Complete' AND oi.delivered_at IS NOT NULL AND oi.delivered_at <= @analysis_cutoff)
       OR (oi.status = 'Returned' AND oi.returned_at IS NOT NULL AND oi.returned_at <= @analysis_cutoff))
GROUP BY p.name ORDER BY sales DESC
```

Question: Average spend per customer by US state, year to date.
```sql
SELECT u.state, COUNT(DISTINCT oi.user_id) AS customers,
       ROUND(SUM(oi.sale_price), 2) AS sales,
       ROUND(SAFE_DIVIDE(SUM(oi.sale_price), COUNT(DISTINCT oi.user_id)), 2) AS spend_per_customer
FROM order_items oi JOIN users u ON u.id = oi.user_id
JOIN orders o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete' AND u.country = 'United States'
  AND oi.delivered_at IS NOT NULL AND oi.delivered_at <= @analysis_cutoff
  AND EXTRACT(YEAR FROM o.created_at) = EXTRACT(YEAR FROM DATE(@analysis_cutoff))
  AND o.created_at <= @analysis_cutoff
GROUP BY u.state ORDER BY spend_per_customer DESC
```
