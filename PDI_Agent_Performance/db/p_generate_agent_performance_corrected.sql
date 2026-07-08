-- ============================================================================
-- p_generate_agent_performance  —  CORRECTED metric version (two-window)
-- ----------------------------------------------------------------------------
-- Kept deliberately in sync with the Python pipeline (app/sql/queries.py) so the
-- two can be cross-validated by validation/validate_agent_performance.py.
--
-- ONE TABLE, TWO CLOCKS:
--   * ACTIVITY counts (live / sold / turnaround) on a RECENT window
--     (first_published within 365 days)  -> is_recent flag.
--   * REALISED sold-vs-ORIGINAL-asking on a MATURE window (24 months), because
--     Land-Registry (PPD) data lags Sold STC by many months. Coverage rises from
--     ~0% at 0-3 months to ~60% at 18-24 months, so the price metric reaches back.
--
--   avg_difference_in_percentage = (PPD sold price − ORIGINAL asking) / ORIGINAL asking * 100
--     * ORIGINAL asking = listed_price, recovered from `price` only when
--       listed_price is missing/a placeholder (the "£1" hiding trick).
--     * sold price = Land Registry (last_transaction_soldPrice).
--     * counted only when the sale registered AFTER go-live (recency guard).
--     * ANOMALIES ARE EXCLUDED FROM THE AVERAGE BUT THE LISTING IS STILL COUNTED.
--
-- CHANGES vs the live procedure are marked  >>> CHANGE.  Intentional difference
-- from Python: this reports the MEAN of valid variations, Python the MEDIAN
-- (MySQL has no MEDIAN aggregate inside GROUP BY). They should be close; a large
-- gap flags a data problem.
--
-- PREREQUISITE: target table must carry no_of_sold_mature, no_of_sold_with_soldprice,
-- no_of_price_recovered and avg_difference_in_percentage widened to DECIMAL(6,2)
-- (see db/migrations/2026-07-08_agent_performance_corrected_metric.sql).
--
-- KNOWN REMAINING GAP: pulls only live + sold, so does NOT compute no_of_listings /
-- no_of_withdrawn_listing (the Python pipeline does). Align later if needed.
-- ============================================================================

-- Fix collation mismatch (1271): stamp utf8mb4_0900_ai_ci onto all procedure
-- string variables so they match PDI_PortalsData table columns at runtime.
-- Run this file as a single script so these SETs apply before CREATE PROCEDURE.
SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci;
SET collation_connection = 'utf8mb4_0900_ai_ci';

DELIMITER $$
CREATE DEFINER=`admin`@`%` PROCEDURE `PDI_PortalsData`.`p_generate_agent_performance_corrected`(IN p_runForDurationInSeconds INT)
BEGIN
    DECLARE v_stop_loop BOOLEAN DEFAULT false;
    DECLARE var_agent_id INT;
    DECLARE tmp_property_details_history_name VARCHAR(100);
    DECLARE tmp_property_details_zoopla_history_name VARCHAR(100);

    DECLARE start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
    DECLARE duration INT DEFAULT p_runForDurationInSeconds;
    DECLARE v_last_processed_id INT DEFAULT 0;
    DECLARE v_temp_table_name VARCHAR(100);
    DECLARE v_process_complete TINYINT DEFAULT 0;
    DECLARE v_control_table_id INT DEFAULT 0;

    SELECT id, last_processed_id, temp_table_name
    INTO v_control_table_id, v_last_processed_id, v_temp_table_name
    FROM PDI_PortalsData.pdi_procedure_logs
    WHERE procedure_name = 'p_generate_agent_performance'
      AND process_complete = false
    ORDER BY id DESC
    LIMIT 1;

    IF v_temp_table_name IS NULL THEN
        SELECT CONCAT('PDI_PortalsData.pdi_agent_performance_dnd_', DATE_FORMAT(current_timestamp(), "%d%m%Y%H%i%S")) INTO v_temp_table_name;
        SET @sql = CONCAT('CREATE TABLE ', v_temp_table_name, ' LIKE PDI_PortalsData.pdi_agent_performance_proc_v2;');
        PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

        INSERT INTO PDI_PortalsData.pdi_procedure_logs (procedure_name, last_processed_id, temp_table_name, process_complete)
        VALUES ('p_generate_agent_performance', 0, v_temp_table_name, false);

        SELECT id, last_processed_id, temp_table_name
        INTO v_control_table_id, v_last_processed_id, v_temp_table_name
        FROM PDI_PortalsData.pdi_procedure_logs
        WHERE procedure_name = 'p_generate_agent_performance'
          AND process_complete = false
        ORDER BY id DESC
        LIMIT 1;
    END IF;

    SET @v = CONCAT('CREATE OR REPLACE VIEW PDI_PortalsData.tmp_view_p_generate_agent_performance as SELECT a.id FROM PDI_PortalsData.pdi_agent_master a where a.id > ', v_last_processed_id, ' ORDER BY a.id');
    PREPARE stmt FROM @v; EXECUTE stmt; DEALLOCATE PREPARE stmt;

    SELECT CONCAT('tmp_property_details_history_', DATE_FORMAT(current_timestamp(), "%d%m%Y%H%i%S")) INTO tmp_property_details_history_name;
    SET @sql = CONCAT('CREATE TEMPORARY TABLE ', tmp_property_details_history_name, ' SELECT pdh.listing_id, pdh.old_value, pdh.new_value, pdh.changed_at
                        FROM PDI_PortalsData.property_details_history pdh
                        INNER JOIN (SELECT listing_id, MAX(id) AS id FROM PDI_PortalsData.property_details_history
                                    WHERE column_name = \'display_status\' GROUP BY listing_id) latest ON pdh.id = latest.id
                        WHERE pdh.new_value IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\')');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

    SELECT CONCAT('tmp_property_details_zoopla_history_', DATE_FORMAT(current_timestamp(), "%d%m%Y%H%i%S")) INTO tmp_property_details_zoopla_history_name;
    SET @sql = CONCAT('CREATE TEMPORARY TABLE ', tmp_property_details_zoopla_history_name, ' SELECT pdh.listing_id, pdh.old_value, pdh.new_value, pdh.changed_at
                FROM PDI_PortalsData.property_details_zoopla_history pdh
                INNER JOIN (SELECT listing_id, MAX(id) AS id FROM PDI_PortalsData.property_details_zoopla_history
                            WHERE column_name = \'display_status\' GROUP BY listing_id) latest ON pdh.id = latest.id
                WHERE pdh.new_value IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\')');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

    SET @sql = CONCAT('CREATE INDEX `idx_listing_id` ON ', tmp_property_details_history_name, ' (listing_id);');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
    SET @sql = CONCAT('CREATE INDEX `idx_listing_id` ON ', tmp_property_details_zoopla_history_name, ' (listing_id);');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

    REPEAT
        SELECT id INTO var_agent_id
        FROM PDI_PortalsData.tmp_view_p_generate_agent_performance
        WHERE id > v_last_processed_id
        ORDER BY id
        LIMIT 1;

        IF var_agent_id IS NULL THEN
            UPDATE PDI_PortalsData.pdi_procedure_logs SET process_complete = true WHERE id = v_control_table_id;
            SET v_process_complete = 1;
            SET v_stop_loop = true;
        ELSE
            SET @sql = CONCAT('Insert into ', v_temp_table_name,
            -- >>> CHANGE: added no_of_sold_mature, no_of_sold_with_soldprice, no_of_price_recovered
            '(agent_name,agent_address,agent_logo,no_of_live_listings,no_of_sold_listings,
              no_of_sold_mature,no_of_sold_with_soldprice,no_of_price_recovered,avg_difference_in_percentage,turnaround_days)
            Select * from
            (Select
                subdata.agent_name as agent_name,
                subdata.agent_address as agent_address,
                max(subdata.agent_logo) as agent_logo,
                -- >>> CHANGE: activity counts on the RECENT window (is_recent)
                CAST(sum(subdata.no_of_live_listing) as UNSIGNED) no_of_live_listings,
                CAST(sum(subdata.no_of_sold_recent) as UNSIGNED) no_of_sold_listings,
                -- >>> CHANGE: mature-window sold count = coverage denominator
                CAST(sum(subdata.no_of_sold_mature) as UNSIGNED) no_of_sold_mature,
                -- >>> CHANGE: coverage — mature sold listings with a usable Land-Registry price
                CAST(sum(subdata.valid_for_avg) as UNSIGNED) no_of_sold_with_soldprice,
                CAST(sum(subdata.asking_recovered) as UNSIGNED) no_of_price_recovered,
                -- >>> CHANGE: MEAN over VALID variations only; divide by coverage, NOT sold count.
                -- >>> Widened to DECIMAL(6,2). Listings are NO LONGER dropped from the counts.
                CAST(sum(IF(subdata.valid_for_avg=1, subdata.difference_in_percentage, 0))
                     / NULLIF(sum(subdata.valid_for_avg),0) as DECIMAL(6,2)) avg_difference_in_percentage,
                -- >>> CHANGE: turnaround over RECENT sold listings (removed the arbitrary >45 gate)
                CAST(round(sum(subdata.turnaround_days) / NULLIF(sum(subdata.no_of_sold_recent),0)) as UNSIGNED) turnaround_days
            FROM
            (
            WITH T_Combine AS (
            SELECT \'Rightmove\' as portal,
                        COALESCE(pam.agent_name_zl, pam.agent_name_rm) AS agent_name,
                        COALESCE(pam.address_zl, pam.address_rm) AS agent_address,
                        pam.agent_logo,
                        p.listing_id,
                        lower(property_type) as property_type,
                        first_published_date,
                        COALESCE(pdi_ppd_last_transaction_soldPrice, last_transaction_soldPrice) last_transaction_soldPrice,
                        COALESCE(pdi_ppd_last_transaction_date, last_transaction_date) last_transaction_date,
                        sold_status_history.changed_at sold_stc_date,
                        postcode, num_bedrooms, price,
                        listed_price,                         -- >>> CHANGE: original asking carried through
                        -- >>> CHANGE: RECENT flag (activity window)
                        IF(first_published_date >= DATE_SUB(CURDATE(), INTERVAL 365 DAY), 1, 0) AS is_recent,
                        outcode, listing_status, display_status, status
                    FROM PDI_PortalsData.property_details p
                    JOIN PDI_PortalsData.pdi_agent_master pam ON (p.agent_name = pam.agent_name_rm AND p.agent_address = pam.address_rm)
                    LEFT JOIN ', tmp_property_details_history_name, ' sold_status_history ON p.listing_id = sold_status_history.listing_id
                    -- >>> CHANGE: widened pull to 24 months (mature price window)
                    WHERE pam.id = ', var_agent_id, ' AND p.first_published_date BETWEEN DATE_SUB(CURDATE(), INTERVAL 24 MONTH) AND CURDATE()
                        AND p.residential = \'YES\' AND p.commercial = \'NO\'
                        AND p.listing_status IN (\'sale\',\'new-homes\')
                        AND (p.status in (\'for_sale\') OR p.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\'))
                UNION ALL
                    SELECT \'Zoopla\' as portal,
                            COALESCE(pam.agent_name_zl, pam.agent_name_rm) AS agent_name,
                            COALESCE(pam.address_zl, pam.address_rm) AS agent_address,
                            pam.agent_logo,
                            p.listing_id,
                            lower(property_type) as property_type,
                            first_published_date,
                            COALESCE(pdi_ppd_last_transaction_soldPrice, last_transaction_soldPrice) last_transaction_soldPrice,
                            COALESCE(pdi_ppd_last_transaction_date, last_transaction_date) last_transaction_date,
                            sold_status_history.changed_at sold_stc_date,
                            postcode, num_bedrooms, price,
                            listed_price,                     -- >>> CHANGE
                            IF(first_published_date >= DATE_SUB(CURDATE(), INTERVAL 365 DAY), 1, 0) AS is_recent,  -- >>> CHANGE
                            outcode, listing_status, display_status, status
                    FROM PDI_PortalsData.property_details_zoopla p
                    JOIN PDI_PortalsData.pdi_agent_master pam ON (p.agent_name = pam.agent_name_zl AND p.agent_address = pam.address_zl)
                    LEFT JOIN ', tmp_property_details_zoopla_history_name, ' sold_status_history ON p.listing_id = sold_status_history.listing_id
                    WHERE pam.id = ', var_agent_id, ' AND p.first_published_date BETWEEN DATE_SUB(CURDATE(), INTERVAL 24 MONTH) AND CURDATE()  -- >>> CHANGE
                        AND p.category = \'residential\'
                        AND p.listing_status IN (\'sale\',\'new-homes\')
                        AND (p.status in (\'for_sale\') OR p.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\'))
            )
            Select
                    main_union.agent_name, main_union.agent_address, main_union.agent_logo,
                    main_union.postcode, main_union.property_type, main_union.num_bedrooms,
                    main_union.first_published_date, main_union.last_transaction_date,
                    main_union.price, main_union.listed_price, main_union.last_transaction_soldPrice,
                    main_union.display_status,
                    -- >>> CHANGE: live only counts on the RECENT window
                    IF(main_union.status = \'for_sale\' AND main_union.is_recent = 1, 1, 0) no_of_live_listing,
                    -- >>> CHANGE: RECENT sold (activity)
                    IF(main_union.is_recent = 1 AND main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\') AND
                        COALESCE(main_union.sold_stc_date, main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date, 1, 0) no_of_sold_recent,
                    -- >>> CHANGE: MATURE sold (coverage denominator, full 24-month window)
                    IF(main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\') AND
                        COALESCE(main_union.sold_stc_date, main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date, 1, 0) no_of_sold_mature,
                    -- >>> CHANGE: variation = (ACTUAL sold − ORIGINAL asking) / ORIGINAL asking; recency-guarded; NULL when no usable price
                    IF(main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\')
                       AND COALESCE(main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date
                       AND main_union.last_transaction_soldPrice > 0
                       AND IF(main_union.listed_price > 1000, main_union.listed_price, IF(main_union.price > 1000, main_union.price, NULL)) IS NOT NULL,
                       ROUND(((main_union.last_transaction_soldPrice - IF(main_union.listed_price > 1000, main_union.listed_price, main_union.price)) * 100)
                             / IF(main_union.listed_price > 1000, main_union.listed_price, main_union.price), 2),
                       NULL) AS difference_in_percentage,
                    -- >>> CHANGE: 1 when this mature sold listing has a usable Land-Registry price (coverage)
                    IF(main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\')
                       AND COALESCE(main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date
                       AND main_union.last_transaction_soldPrice > 0
                       AND IF(main_union.listed_price > 1000, main_union.listed_price, IF(main_union.price > 1000, main_union.price, NULL)) IS NOT NULL, 1, 0) AS valid_for_avg,
                    -- >>> CHANGE: 1 when original asking recovered from a placeholder price
                    IF((main_union.listed_price IS NULL OR main_union.listed_price <= 1000) AND main_union.price > 1000, 1, 0) AS asking_recovered,
                    -- >>> CHANGE: turnaround only on RECENT sold
                    IF(main_union.is_recent = 1 AND main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\') AND
                        COALESCE(main_union.sold_stc_date, main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date,
                        DATEDIFF(COALESCE(main_union.sold_stc_date, main_union.last_transaction_date), main_union.first_published_date), 0) AS turnaround_days
            FROM T_Combine main_union
            LEFT JOIN
                (SELECT max(combined_tables.first_published_date) as first_published_date, combined_tables.listing_status,
                        combined_tables.property_type, combined_tables.postcode, combined_tables.num_bedrooms, combined_tables.price, count(*)
                FROM T_Combine AS combined_tables
                GROUP BY combined_tables.postcode, combined_tables.num_bedrooms, combined_tables.price, combined_tables.listing_status, combined_tables.property_type
                HAVING COUNT(*) > 1) duplicate_results
                ON (main_union.postcode = duplicate_results.postcode AND main_union.num_bedrooms = duplicate_results.num_bedrooms
                    AND main_union.price = duplicate_results.price AND main_union.listing_status = duplicate_results.listing_status
                    AND main_union.property_type = duplicate_results.property_type)
                WHERE duplicate_results.postcode IS NULL
            UNION ALL
            Select
                    main_union.agent_name, main_union.agent_address, main_union.agent_logo,
                    main_union.postcode, main_union.property_type, main_union.num_bedrooms,
                    main_union.first_published_date, main_union.last_transaction_date,
                    main_union.price, main_union.listed_price, main_union.last_transaction_soldPrice,
                    main_union.display_status,
                    IF(main_union.status = \'for_sale\' AND main_union.is_recent = 1, 1, 0) no_of_live_listing,
                    IF(main_union.is_recent = 1 AND main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\') AND
                        COALESCE(main_union.sold_stc_date, main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date, 1, 0) no_of_sold_recent,
                    IF(main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\') AND
                        COALESCE(main_union.sold_stc_date, main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date, 1, 0) no_of_sold_mature,
                    IF(main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\')
                       AND COALESCE(main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date
                       AND main_union.last_transaction_soldPrice > 0
                       AND IF(main_union.listed_price > 1000, main_union.listed_price, IF(main_union.price > 1000, main_union.price, NULL)) IS NOT NULL,
                       ROUND(((main_union.last_transaction_soldPrice - IF(main_union.listed_price > 1000, main_union.listed_price, main_union.price)) * 100)
                             / IF(main_union.listed_price > 1000, main_union.listed_price, main_union.price), 2),
                       NULL) AS difference_in_percentage,
                    IF(main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\')
                       AND COALESCE(main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date
                       AND main_union.last_transaction_soldPrice > 0
                       AND IF(main_union.listed_price > 1000, main_union.listed_price, IF(main_union.price > 1000, main_union.price, NULL)) IS NOT NULL, 1, 0) AS valid_for_avg,
                    IF((main_union.listed_price IS NULL OR main_union.listed_price <= 1000) AND main_union.price > 1000, 1, 0) AS asking_recovered,
                    IF(main_union.is_recent = 1 AND main_union.display_status IN (\'Sold STC\',\'Under offer\',\'Reserved\',\'Sold STCM\',\'Sold subject to\',\'Under offer\',\'Sold subject to contract\') AND
                        COALESCE(main_union.sold_stc_date, main_union.last_transaction_date, \'1800-01-01\') >= main_union.first_published_date,
                        DATEDIFF(COALESCE(main_union.sold_stc_date, main_union.last_transaction_date), main_union.first_published_date), 0) AS turnaround_days
            FROM T_Combine main_union
            JOIN
                (SELECT max(combined_tables.first_published_date) as first_published_date, combined_tables.listing_status,
                        combined_tables.property_type, combined_tables.postcode, combined_tables.num_bedrooms, combined_tables.price, count(*)
                FROM T_Combine AS combined_tables
                GROUP BY combined_tables.postcode, combined_tables.num_bedrooms, combined_tables.price, combined_tables.listing_status, combined_tables.property_type
                HAVING COUNT(*) > 1) duplicate_results
                ON (main_union.postcode = duplicate_results.postcode AND main_union.num_bedrooms = duplicate_results.num_bedrooms
                    AND main_union.price = duplicate_results.price AND main_union.listing_status = duplicate_results.listing_status
                    AND main_union.property_type = duplicate_results.property_type
                    AND main_union.first_published_date = duplicate_results.first_published_date)
            ) subdata
            -- >>> CHANGE: removed  "where no_of_sold_listing=0 OR (difference between -30 and 30 AND turnaround_days>45)".
            -- >>> That old filter DROPPED anomalous / fast sales from the counts too. Now every listing is
            -- >>> counted; only the price VALUE is excluded from the average (via valid_for_avg above).
            Group by subdata.agent_name, subdata.agent_address
            order by no_of_live_listings desc) agentPerformance
            ON DUPLICATE KEY UPDATE
            agent_logo = agentPerformance.agent_logo,
            no_of_live_listings = agentPerformance.no_of_live_listings,
            no_of_sold_listings = agentPerformance.no_of_sold_listings,
            no_of_sold_mature = agentPerformance.no_of_sold_mature,
            no_of_sold_with_soldprice = agentPerformance.no_of_sold_with_soldprice,
            no_of_price_recovered = agentPerformance.no_of_price_recovered,
            avg_difference_in_percentage = agentPerformance.avg_difference_in_percentage,
            turnaround_days = agentPerformance.turnaround_days,
            update_dt = now();');
            PREPARE stmt FROM @sql; EXECUTE stmt; COMMIT; DEALLOCATE PREPARE stmt;

            UPDATE PDI_PortalsData.pdi_procedure_logs SET last_processed_id = var_agent_id WHERE id = v_control_table_id;
            SET v_last_processed_id = var_agent_id;
        END IF;

        IF TIMESTAMPDIFF(SECOND, start_time, CURRENT_TIMESTAMP) >= duration THEN
            SET v_stop_loop = true;
        END IF;
    UNTIL v_stop_loop END REPEAT;

    -- merge temp -> main
    SET @v = CONCAT('
        INSERT INTO PDI_PortalsData.pdi_agent_performance
        (agent_name, agent_address, agent_logo, no_of_live_listings, no_of_sold_listings,
         no_of_sold_mature, no_of_sold_with_soldprice, no_of_price_recovered, avg_difference_in_percentage, turnaround_days)
        SELECT agent_name, agent_address, agent_logo, no_of_live_listings, no_of_sold_listings,
               no_of_sold_mature, no_of_sold_with_soldprice, no_of_price_recovered, avg_difference_in_percentage, turnaround_days
        FROM ', v_temp_table_name, ' AS agent_temp
        ON DUPLICATE KEY UPDATE
            agent_logo = agent_temp.agent_logo,
            no_of_live_listings = agent_temp.no_of_live_listings,
            no_of_sold_listings = agent_temp.no_of_sold_listings,
            no_of_sold_mature = agent_temp.no_of_sold_mature,
            no_of_sold_with_soldprice = agent_temp.no_of_sold_with_soldprice,
            no_of_price_recovered = agent_temp.no_of_price_recovered,
            avg_difference_in_percentage = agent_temp.avg_difference_in_percentage,
            turnaround_days = agent_temp.turnaround_days,
            update_dt = NOW();
    ');
    PREPARE stmt FROM @v; EXECUTE stmt; DEALLOCATE PREPARE stmt;

    SET @v = CONCAT('Truncate table ', v_temp_table_name, ';');
    PREPARE stmt FROM @v; EXECUTE stmt; DEALLOCATE PREPARE stmt;

    IF v_process_complete = 1 THEN
        SET @sql = CONCAT('Drop table ', v_temp_table_name, ';');
        PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
    END IF;

    DROP VIEW IF EXISTS PDI_PortalsData.tmp_view_p_generate_agent_performance;
    SET @sql = CONCAT('DROP TABLE IF EXISTS ', tmp_property_details_history_name, ', ', tmp_property_details_zoopla_history_name, ';');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
END$$
DELIMITER ;
