DROP PROCEDURE IF EXISTS PDI_PortalsData.p_generate_agent_master_13July26;
DELIMITER $$
CREATE DEFINER=`admin`@`%` PROCEDURE `PDI_PortalsData`.`p_generate_agent_master_13July26`()
BEGIN
	DECLARE done INT DEFAULT FALSE;
    DECLARE var_outcode VARCHAR(10);
    DECLARE tmp_pdi_agent_master_name VARCHAR(100);
    DECLARE tmp_table_count INT DEFAULT 0;
    -- Declare cursor for fetching data
    DECLARE cur CURSOR FOR SELECT p.postcode as outcode FROM PropDealsIns.Postcode_sectors p;
    
    -- Declare continue handler to exit loop when no more rows
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;
    
    SELECT concat('PDI_PortalsData.pdi_agent_master_', DATE_FORMAT(current_timestamp(), "%d%m%Y%H%i%S")) into tmp_pdi_agent_master_name;
    SET @sql = CONCAT('CREATE TABLE ', tmp_pdi_agent_master_name, ' LIKE PDI_PortalsData.pdi_agent_master;');
	PREPARE stmt FROM @sql;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
    
    -- create probable match if not exists
    CREATE TABLE IF NOT EXISTS PDI_PortalsData.pdi_agent_master_probable_match LIKE PDI_PortalsData.pdi_agent_master;
    
    -- get existing matching list
    SET @sql = CONCAT('
    INSERT IGNORE INTO ',tmp_pdi_agent_master_name,'
    (
		agent_master_name,
		agent_name_rm,
		agent_name_zl,
		address_rm,
		address_zl,
		agent_logo, 
		create_dt, 
		update_dt, 
		matching_algo
	)
	SELECT p.agent_master_name,
		p.agent_name_rm,
		p.agent_name_zl,
		p.address_rm,
		p.address_zl,
		p.agent_logo, 
		p.create_dt, 
		p.update_dt, 
		p.matching_algo 
	FROM PDI_PortalsData.pdi_agent_master p
	WHERE p.matching_algo IN (
		-- \'SAME_AGENT_NAME\', 
        -- \'NORMALISED_AGENT_NAME_REMOVED_SPACE\', 
        \'MANUAL\'
	)
    UNION ALL
    SELECT p.agent_master_name,
		p.agent_name_rm,
		p.agent_name_zl,
		p.address_rm,
		p.address_zl,
		p.agent_logo, 
		p.create_dt, 
		p.update_dt, 
		p.matching_algo FROM PDI_PortalsData.pdi_agent_master_probable_match p
	WHERE p.matching_algo IN (
        \'MANUAL\'
	)
    ');
	PREPARE stmt FROM @sql;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
    
    -- remove records which we have used already
    SET SQL_SAFE_UPDATES = 0;
    DELETE FROM PDI_PortalsData.pdi_agent_master_probable_match WHERE matching_algo NOT IN ('NOT_SAME');
    SET SQL_SAFE_UPDATES = 1;
    
    -- Open the cursor
    OPEN cur;
    -- Start looping through the results
    loop_fetch: LOOP
        -- Fetch the next row into variables
        FETCH cur INTO var_outcode;
        -- Check if there are no more rows to fetch
        IF done THEN
            LEAVE loop_fetch;
        END IF;
        
        -- Create optimized agent summary tables with proper column types
        DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_agents_summary;
		SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_rm_agents_summary AS
			SELECT 
				p.agent_name,
				p.agent_address,
				MAX(p.agent_logo) as agent_logo,
				COUNT(*) as property_count,
				PDI_PortalsData.normalize_agent_name(p.agent_name) as normalized_agent_name,
				LOWER(REGEXP_REPLACE(PDI_PortalsData.normalize_agent_name(p.agent_name), \'[^A-Za-z0-9]\', \'\')) as clean_agent_name
			FROM PDI_PortalsData.property_details p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_rm AND p.agent_address = ptmp.address_rm
			WHERE outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_rm IS NULL
			GROUP BY p.agent_name, p.agent_address
			HAVING property_count >= 1
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
        DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_agents_summary;
		SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_zl_agents_summary AS
			SELECT 
				p.agent_name,
				p.agent_address,
				MAX(p.agent_logo) as agent_logo,
				COUNT(*) as property_count,
				PDI_PortalsData.normalize_agent_name(p.agent_name) as normalized_agent_name,
				LOWER(REGEXP_REPLACE(PDI_PortalsData.normalize_agent_name(p.agent_name), \'[^A-Za-z0-9]\', \'\')) as clean_agent_name
			FROM PDI_PortalsData.property_details_zoopla p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
			WHERE outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_zl IS NULL
			GROUP BY p.agent_name, p.agent_address
			HAVING property_count >= 1
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
		-- Add indexes for better performance
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD PRIMARY KEY (agent_name(100), agent_address(100));
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD INDEX idx_normalized (normalized_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD INDEX idx_clean (clean_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD PRIMARY KEY (agent_name(100), agent_address(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD INDEX idx_normalized (normalized_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD INDEX idx_clean (clean_agent_name(100));

		-- Step 3: Create separate property signature tables for RM and ZL (avoids MySQL temp table reopen limitation)
		DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_property_signatures;
        SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_rm_property_signatures AS
			SELECT 
				CONCAT(p.postcode, \'|\', p.price, \'|\', p.num_bedrooms, \'|\', 
					   IF(p.listing_status = \'new-homes\', \'sale\', p.listing_status)) as property_signature,
				p.agent_name,
				p.agent_address,
				p.full_property_address
			FROM PDI_PortalsData.property_details p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_rm AND p.agent_address = ptmp.address_rm
			WHERE p.outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_rm IS NULL
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;

		DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_property_signatures;
        SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_zl_property_signatures AS
			SELECT
				CONCAT(p.postcode, \'|\', p.price, \'|\', p.num_bedrooms, \'|\',
					   IF(p.listing_status = \'new-homes\', \'sale\', p.listing_status)) as property_signature,
				p.agent_name,
				p.agent_address,
				p.full_property_address
			FROM PDI_PortalsData.property_details_zoopla p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
			WHERE p.outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_zl IS NULL
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;

		ALTER TABLE PDI_PortalsData.temp_rm_property_signatures ADD INDEX idx_signature (property_signature);
		ALTER TABLE PDI_PortalsData.temp_rm_property_signatures ADD INDEX idx_agent (agent_name, agent_address);
		ALTER TABLE PDI_PortalsData.temp_zl_property_signatures ADD INDEX idx_signature (property_signature);
		ALTER TABLE PDI_PortalsData.temp_zl_property_signatures ADD INDEX idx_agent (agent_name, agent_address);


		-- Step 4: Find agent matches via property signatures (much faster than large joins)
		DROP TABLE IF EXISTS PDI_PortalsData.temp_agent_matches;
		CREATE TEMPORARY TABLE PDI_PortalsData.temp_agent_matches AS
		SELECT DISTINCT
			rm_sigs.agent_name as rm_agent_name,
			rm_sigs.agent_address as rm_agent_address,
			zl_sigs.agent_name as zl_agent_name,
			zl_sigs.agent_address as zl_agent_address,
			COUNT(*) as matching_properties
		FROM PDI_PortalsData.temp_rm_property_signatures rm_sigs
		INNER JOIN PDI_PortalsData.temp_zl_property_signatures zl_sigs ON rm_sigs.property_signature = zl_sigs.property_signature
		GROUP BY rm_sigs.agent_name, rm_sigs.agent_address, zl_sigs.agent_name, zl_sigs.agent_address
		HAVING matching_properties >= 1;

		-- Step 5: Strategy 1 - Same Agent Name matches
		SET @sql = CONCAT('
			INSERT IGNORE INTO ', tmp_pdi_agent_master_name, '
			(agent_master_name, agent_name_rm, agent_name_zl, address_rm, address_zl, agent_logo, matching_algo)
			SELECT DISTINCT
				COALESCE(rm.agent_name, zl.agent_name) as agent_master_name,
				rm.agent_name as agent_name_rm,
				zl.agent_name as agent_name_zl,
				rm.agent_address as address_rm,
				zl.agent_address as address_zl,
				COALESCE(zl.agent_logo, rm.agent_logo) as agent_logo,
				''SAME_AGENT_NAME'' as matching_algo
			FROM PDI_PortalsData.temp_rm_agents_summary rm
			INNER JOIN PDI_PortalsData.temp_zl_agents_summary zl ON PDI_PortalsData.are_strings_similar(rm.normalized_agent_name, zl.normalized_agent_name)
			INNER JOIN PDI_PortalsData.temp_agent_matches am ON (
				am.rm_agent_name = rm.agent_name 
				AND am.rm_agent_address = rm.agent_address
				AND am.zl_agent_name = zl.agent_name 
				AND am.zl_agent_address = zl.agent_address
			)
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
		
        -- Step 6: Strategy 2 - Normalized Agent Name (Space Removed) matches
		SET @sql = CONCAT('
			INSERT IGNORE INTO ', tmp_pdi_agent_master_name, '
			(agent_master_name, agent_name_rm, agent_name_zl, address_rm, address_zl, agent_logo, matching_algo)
			SELECT DISTINCT
				COALESCE(rm.agent_name, zl.agent_name) as agent_master_name,
				rm.agent_name as agent_name_rm,
				zl.agent_name as agent_name_zl,
				rm.agent_address as address_rm,
				zl.agent_address as address_zl,
				COALESCE(zl.agent_logo, rm.agent_logo) as agent_logo,
				''NORMALISED_AGENT_NAME_REMOVED_SPACE'' as matching_algo
			FROM PDI_PortalsData.temp_rm_agents_summary rm
			INNER JOIN PDI_PortalsData.temp_zl_agents_summary zl ON PDI_PortalsData.are_strings_similar(rm.clean_agent_name, zl.clean_agent_name)
			INNER JOIN PDI_PortalsData.temp_agent_matches am ON (
				am.rm_agent_name = rm.agent_name 
				AND am.rm_agent_address = rm.agent_address
				AND am.zl_agent_name = zl.agent_name 
				AND am.zl_agent_address = zl.agent_address
			)
			WHERE NOT EXISTS (
				SELECT 1 FROM ', tmp_pdi_agent_master_name, ' existing
				WHERE existing.agent_name_rm = rm.agent_name 
				  AND existing.address_rm = rm.agent_address
				  AND existing.agent_name_zl = zl.agent_name 
				  AND existing.address_zl = zl.agent_address
			)
		');
        PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
    END LOOP;
    
    -- Close the cursor
    CLOSE cur;
    
    
    
    -- add probably match RM and ZL 
    
    -- Reset the done flag for second loop
    SET done = FALSE;
    
    -- Open the cursor
    OPEN cur;
    -- Start looping through the results
    loop_fetch: LOOP
        -- Fetch the next row into variables
        FETCH cur INTO var_outcode;
        -- Check if there are no more rows to fetch
        IF done THEN
            LEAVE loop_fetch;
        END IF;
        
        -- Create optimized agent summary tables with proper column types
        DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_agents_summary;
		SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_rm_agents_summary AS
			SELECT 
				p.agent_name,
				p.agent_address,
				MAX(p.agent_logo) as agent_logo,
				COUNT(*) as property_count,
				PDI_PortalsData.normalize_agent_name(p.agent_name) as normalized_agent_name,
				LOWER(REGEXP_REPLACE(PDI_PortalsData.normalize_agent_name(p.agent_name), \'[^A-Za-z0-9]\', \'\')) as clean_agent_name
			FROM PDI_PortalsData.property_details p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_rm AND p.agent_address = ptmp.address_rm
			WHERE outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_rm IS NULL
			GROUP BY p.agent_name, p.agent_address
			HAVING property_count >= 1
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
        DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_agents_summary;
		SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_zl_agents_summary AS
			SELECT 
				p.agent_name,
				p.agent_address,
				MAX(p.agent_logo) as agent_logo,
				COUNT(*) as property_count,
				PDI_PortalsData.normalize_agent_name(p.agent_name) as normalized_agent_name,
				LOWER(REGEXP_REPLACE(PDI_PortalsData.normalize_agent_name(p.agent_name), \'[^A-Za-z0-9]\', \'\')) as clean_agent_name
			FROM PDI_PortalsData.property_details_zoopla p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
			WHERE outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_zl IS NULL
			GROUP BY p.agent_name, p.agent_address
			HAVING property_count >= 1
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
		-- Add indexes for better performance
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD PRIMARY KEY (agent_name(100), agent_address(100));
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD INDEX idx_normalized (normalized_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD INDEX idx_clean (clean_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD PRIMARY KEY (agent_name(100), agent_address(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD INDEX idx_normalized (normalized_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD INDEX idx_clean (clean_agent_name(100));

		-- Step 3: Create separate property signature tables for RM and ZL (avoids MySQL temp table reopen limitation)
		DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_property_signatures;
        SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_rm_property_signatures AS
			SELECT 
				CONCAT(p.postcode, \'|\', p.price, \'|\', p.num_bedrooms, \'|\', 
					   IF(p.listing_status = \'new-homes\', \'sale\', p.listing_status)) as property_signature,
				p.agent_name,
				p.agent_address,
				p.full_property_address
			FROM PDI_PortalsData.property_details p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_rm AND p.agent_address = ptmp.address_rm
			WHERE p.outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_rm IS NULL
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
		DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_property_signatures;
        SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_zl_property_signatures AS
			SELECT 
				CONCAT(p.postcode, \'|\', p.price, \'|\', p.num_bedrooms, \'|\', 
					   IF(p.listing_status = \'new-homes\', \'sale\', p.listing_status)) as property_signature,
				p.agent_name,
				p.agent_address,
				p.full_property_address
			FROM PDI_PortalsData.property_details_zoopla p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
			WHERE p.outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_zl IS NULL
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
		ALTER TABLE PDI_PortalsData.temp_rm_property_signatures ADD INDEX idx_signature (property_signature);
		ALTER TABLE PDI_PortalsData.temp_rm_property_signatures ADD INDEX idx_agent (agent_name, agent_address);
		ALTER TABLE PDI_PortalsData.temp_zl_property_signatures ADD INDEX idx_signature (property_signature);
		ALTER TABLE PDI_PortalsData.temp_zl_property_signatures ADD INDEX idx_agent (agent_name, agent_address);
        
        -- Strategy 3 - Full Address matches (probable matches)
		INSERT INTO PDI_PortalsData.pdi_agent_master_probable_match
		(agent_master_name, agent_name_rm, agent_name_zl, address_rm, address_zl, agent_logo, matching_algo)
		SELECT DISTINCT
			COALESCE(rm_sigs.agent_name, zl_sigs.agent_name) as agent_master_name,
			rm_sigs.agent_name as agent_name_rm,
			zl_sigs.agent_name as agent_name_zl,
			rm_sigs.agent_address as address_rm,
			zl_sigs.agent_address as address_zl,
			COALESCE(zl_sum.agent_logo, rm_sum.agent_logo) as agent_logo,
			'SAME_FULL_ADDRESS' as matching_algo
		FROM PDI_PortalsData.temp_rm_property_signatures rm_sigs
		INNER JOIN PDI_PortalsData.temp_zl_property_signatures zl_sigs ON (
			rm_sigs.property_signature = zl_sigs.property_signature
			AND rm_sigs.full_property_address IS NOT NULL
			AND zl_sigs.full_property_address IS NOT NULL
			AND PDI_PortalsData.are_strings_similar(rm_sigs.full_property_address, zl_sigs.full_property_address)
		)
		LEFT JOIN PDI_PortalsData.temp_rm_agents_summary rm_sum ON (
			rm_sum.agent_name = rm_sigs.agent_name 
			AND rm_sum.agent_address = rm_sigs.agent_address
		)
		LEFT JOIN PDI_PortalsData.temp_zl_agents_summary zl_sum ON (
			zl_sum.agent_name = zl_sigs.agent_name 
			AND zl_sum.agent_address = zl_sigs.agent_address
		)
		WHERE NOT EXISTS (
			SELECT 1 FROM PDI_PortalsData.pdi_agent_master_probable_match am
			WHERE am.agent_name_rm = rm_sigs.agent_name 
			  AND am.address_rm = rm_sigs.agent_address
			  AND am.agent_name_zl = zl_sigs.agent_name 
			  AND am.address_zl = zl_sigs.agent_address
		);
        
    END LOOP;
    
    -- Close the cursor
    CLOSE cur;
    
    
    
    
    -- add remaining agents RM and ZL 
    
    -- Reset the done flag for second loop
    SET done = FALSE;
    
    -- Open the cursor
    OPEN cur;
    -- Start looping through the results
    loop_fetch: LOOP
        -- Fetch the next row into variables
        FETCH cur INTO var_outcode;
        -- Check if there are no more rows to fetch
        IF done THEN
            LEAVE loop_fetch;
        END IF;
        
        -- Create optimized agent summary tables with proper column types
        DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_agents_summary;
		SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_rm_agents_summary AS
			SELECT 
				p.agent_name,
				p.agent_address,
				MAX(p.agent_logo) as agent_logo,
				COUNT(*) as property_count,
				PDI_PortalsData.normalize_agent_name(p.agent_name) as normalized_agent_name,
				LOWER(REGEXP_REPLACE(PDI_PortalsData.normalize_agent_name(p.agent_name), \'[^A-Za-z0-9]\', \'\')) as clean_agent_name
			FROM PDI_PortalsData.property_details p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_rm AND p.agent_address = ptmp.address_rm
			WHERE outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_rm IS NULL
			GROUP BY p.agent_name, p.agent_address
			HAVING property_count >= 1
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
        DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_agents_summary;
		SET @sql = CONCAT('
			CREATE TEMPORARY TABLE PDI_PortalsData.temp_zl_agents_summary AS
			SELECT 
				p.agent_name,
				p.agent_address,
				MAX(p.agent_logo) as agent_logo,
				COUNT(*) as property_count,
				PDI_PortalsData.normalize_agent_name(p.agent_name) as normalized_agent_name,
				LOWER(REGEXP_REPLACE(PDI_PortalsData.normalize_agent_name(p.agent_name), \'[^A-Za-z0-9]\', \'\')) as clean_agent_name
			FROM PDI_PortalsData.property_details_zoopla p LEFT JOIN
			',tmp_pdi_agent_master_name,' ptmp ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
			WHERE outcode = \'',var_outcode,'\'
			AND ptmp.agent_name_zl IS NULL
			GROUP BY p.agent_name, p.agent_address
			HAVING property_count >= 1
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
		-- Add indexes for better performance
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD PRIMARY KEY (agent_name(100), agent_address(100));
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD INDEX idx_normalized (normalized_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_rm_agents_summary ADD INDEX idx_clean (clean_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD PRIMARY KEY (agent_name(100), agent_address(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD INDEX idx_normalized (normalized_agent_name(100));
		ALTER TABLE PDI_PortalsData.temp_zl_agents_summary ADD INDEX idx_clean (clean_agent_name(100));

        -- Add remaining unmatched agents
		SET @sql = CONCAT('
			INSERT INTO ', tmp_pdi_agent_master_name, '
			(agent_master_name, agent_name_rm, agent_name_zl, address_rm, address_zl, agent_logo, matching_algo)
			SELECT 
				agent_name as agent_master_name,
				agent_name as agent_name_rm,
				NULL as agent_name_zl,
				agent_address as address_rm,
				NULL as address_zl,
				agent_logo,
				NULL as matching_algo
			FROM PDI_PortalsData.temp_rm_agents_summary rm
			WHERE NOT EXISTS (
				SELECT 1 FROM ', tmp_pdi_agent_master_name, ' existing
				WHERE existing.agent_name_rm = rm.agent_name 
				  AND existing.address_rm = rm.agent_address
			)
			UNION ALL
			SELECT 
				agent_name as agent_master_name,
				NULL as agent_name_rm,
				agent_name as agent_name_zl,
				NULL as address_rm,
				agent_address as address_zl,
				agent_logo,
				NULL as matching_algo
			FROM PDI_PortalsData.temp_zl_agents_summary zl
			WHERE NOT EXISTS (
				SELECT 1 FROM ', tmp_pdi_agent_master_name, ' existing
				WHERE existing.agent_name_zl = zl.agent_name 
				  AND existing.address_zl = zl.agent_address
			)
		');
		PREPARE stmt FROM @sql;
		EXECUTE stmt;
		DEALLOCATE PREPARE stmt;
        
    END LOOP;
    
    -- Close the cursor
    CLOSE cur;
    
    SET @sql = CONCAT('Select count(*) into @tmp_table_count from ', tmp_pdi_agent_master_name, ';');
	PREPARE stmt FROM @sql;
	EXECUTE stmt;
	DEALLOCATE PREPARE stmt;
    
	-- SET tmp_table_count = @tmp_table_count;
	-- IF tmp_table_count > 0 THEN
	-- 	DROP TABLE IF EXISTS PDI_PortalsData.pdi_agent_master_bkp;
	-- 	RENAME TABLE PDI_PortalsData.pdi_agent_master TO PDI_PortalsData.pdi_agent_master_bkp;
	-- 	SET @sql = CONCAT('RENAME TABLE ',tmp_pdi_agent_master_name,' TO PDI_PortalsData.pdi_agent_master;');
	-- 	PREPARE stmt FROM @sql;
	-- 	EXECUTE stmt;
	-- 	DEALLOCATE PREPARE stmt;
	-- END IF;
	
	-- Clean up temporary tables
	DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_agents_summary;
	DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_agents_summary;
	DROP TABLE IF EXISTS PDI_PortalsData.temp_rm_property_signatures;
	DROP TABLE IF EXISTS PDI_PortalsData.temp_zl_property_signatures;
	DROP TABLE IF EXISTS PDI_PortalsData.temp_agent_matches;
    
END$$
DELIMITER ;
