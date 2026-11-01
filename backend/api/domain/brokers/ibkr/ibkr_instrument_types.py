"""Explicit broker contract types with independently comparable NAV components."""

# IBKR defines these contract types; component totals must also match the report.
# https://interactivebrokers.github.io/tws-api/classIBApi_1_1Contract.html
# https://www.ibkrguides.com/reportingreference/reportguide/net%20asset%20value%20(nav)%20summary%20in%20base.htm
SECURITY_COMPONENTS={'STK':'stock','OPT':'options','BOND':'bonds','FUND':'funds','CMDTY':'commodities'}
SECURITY_LABELS={'OPT':'期权','BOND':'债券','FUND':'基金','CMDTY':'商品'}
