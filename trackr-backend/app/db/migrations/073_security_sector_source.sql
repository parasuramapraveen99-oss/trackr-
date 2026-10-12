-- Which list classified security.sector. Two exchanges publish the same SEBI/AMFI
-- harmonised sector taxonomy: NSE in its Nifty Total Market constituent list (about 750
-- companies), BSE in each scrip's header. BSE fills only ISINs NSE's list does not carry;
-- its labels are normalised to NSE's spelling (BSE writes "Media, Entertainment &
-- Publication"). Measured 2026-10-10: 10 of 12 dual-classified holdings identical, 2 only
-- by that comma. NULL for rows set before this column existed.
ALTER TABLE security ADD COLUMN sector_source text;
