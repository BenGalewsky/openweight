-- ----------------------------------------------------------
-- MDB Tools - A library for reading MS Access database files
-- Copyright (C) 2000-2011 Brian Bruns and others.
-- Files in libmdb are licensed under LGPL and the utilities under
-- the GPL, see COPYING.LIB and COPYING files respectively.
-- Check out http://mdbtools.sourceforge.net
-- ----------------------------------------------------------

-- That file uses encoding UTF-8

CREATE TABLE [BAR_Logger]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[ImportID]			Long Integer NOT NULL,
	[Level]			Double,
	[Temperature]			Double,
	[Exclude]			Boolean NOT NULL,
	CONSTRAINT fk_barlogger_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_barlogger_import FOREIGN KEY ([ImportID]) REFERENCES [Imports] ([ID])
);

CREATE TABLE [Benchmarks]
 (
	[ID]			Long Integer,
	[ProjectID]			Long Integer NOT NULL,
	[BmName]			Text (255),
	[Easting]			Double,
	[Northing]			Double,
	[ZoneID]			Long Integer,
	[OrthoHt]			Double,
	[DatumID]			Long Integer,
	[ElevTypeID]			Long Integer,
	[ElevNote]			Text (255),
	[DT]			DateTime,
	[Method]			Text (255),
	[Location]			Text (255),
	[Note]			Text (255),
	CONSTRAINT fk_benchmarks_project FOREIGN KEY ([ProjectID]) REFERENCES [Projects] ([ID]),
	CONSTRAINT fk_benchmarks_zone FOREIGN KEY ([ZoneID]) REFERENCES [LUT_SPZone] ([ID]),
	CONSTRAINT fk_benchmarks_datum FOREIGN KEY ([DatumID]) REFERENCES [LUT_Datum] ([ID]),
	CONSTRAINT fk_benchmarks_elevtype FOREIGN KEY ([ElevTypeID]) REFERENCES [LUT_ElevType] ([ID])
);

CREATE TABLE [DB_Flavor]
 (
	[Flavor]			Text (255) NOT NULL,
	[SplitDate]			DateTime,
	[TitleAppend]			Text (255),
	[TitleBgndColor]			Text (6),
	[MenuBarColor]			Text (6)
);

CREATE TABLE [DL_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[TimeOfDL]			DateTime,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_dlread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_dlread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_dlread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [GW_Logger]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[ImportID]			Long Integer NOT NULL,
	[Level]			Double,
	[Temperature]			Double,
	[xWL]			Boolean NOT NULL,
	[xT]			Boolean NOT NULL,
	[rInitials]			Text (3),
	[rDate]			DateTime,
	CONSTRAINT fk_gwlogger_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_gwlogger_import FOREIGN KEY ([ImportID]) REFERENCES [Imports] ([ID]),
	CONSTRAINT fk_gwlogger_rinitials FOREIGN KEY ([rInitials]) REFERENCES [LUTC_Initials] ([Initials])
);

CREATE TABLE [GW_LoggerAux]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[DO]			Double,
	[DOS]			Double,
	[ORP]			Long Integer,
	[PH]			Double,
	[SPC]			Double,
	[TRB]			Double,
	[TDS]			Double,
	[xDO]			Boolean NOT NULL,
	[xDOS]			Boolean NOT NULL,
	[xORP]			Boolean NOT NULL,
	[xPH]			Boolean NOT NULL,
	[xSPC]			Boolean NOT NULL,
	[xTRB]			Boolean NOT NULL,
	[xTDS]			Boolean NOT NULL,
	CONSTRAINT fk_gwloggeraux_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_gwloggeraux_record FOREIGN KEY ([DateTime], [StationID]) REFERENCES [GW_Logger] ([DateTime], [StationID])
);

CREATE TABLE [GW_Read (placeholder)]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_gwread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_gwread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_gwread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [ImportLog]
 (
	[ID]			Long Integer,
	[DT]			DateTime,
	[Level]			Text (255),
	[Message]			Text (255),
	[Info1]			Text (255),
	[Info2]			Text (255),
	[Info3]			Text (255),
	[UserID]			Text (10)
);

CREATE TABLE [Imports]
 (
	[ID]			Long Integer,
	[ISGS_Num_Filename]			Text (10),
	[VisitID]			Long Integer,
	[Note]			Text (255),
	[StationID]			Long Integer,
	[File]			Text (255),
	[FileSpec]			Text (255),
	[SerialNo]			Text (255),
	[ImportDT]			DateTime,
	[ImportUserID]			Text (10),
	[Completed]			Boolean NOT NULL,
	[Info]			Text (255),
	CONSTRAINT fk_imports_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_imports_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID])
);

CREATE TABLE [LoggerRecordNoteIDs]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[ID]			Long Integer,
	CONSTRAINT fk_loggerrecordnoteids_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_loggerrecordnoteids_note FOREIGN KEY ([ID]) REFERENCES [LoggerRecordNotes] ([ID])
);

CREATE TABLE [LoggerRecordNotes]
 (
	[ID]			Long Integer,
	[Note]			Text (255)
);

CREATE TABLE [LUT_BoringMethod]
 (
	[ID]			Long Integer,
	[BoringMethod]			Text (255)
);

CREATE TABLE [LUT_County_Names]
 (
	[CNTYCODE]			Numeric (3, 0) NOT NULL,
	[CNTYNAME]			Text (13)
);

CREATE TABLE [LUT_Dry_Read_Import]
 (
	[OldStatus]			Text (255),
	[NewStatus]			Text (255),
	[ToComments]			Boolean NOT NULL,
	[ReadingsStopped]			Boolean NOT NULL
);

CREATE TABLE [LUT_ElevType]
 (
	[ID]			Long Integer NOT NULL,
	[ElevType]			Text (10)
);

CREATE TABLE [LUT_LocationType]
 (
	[ID]			Long Integer,
	[LocationType]			Text (255)
);

CREATE TABLE [LUT_SiteType]
 (
	[ID]			Long Integer,
	[SiteType]			Text (255)
);

CREATE TABLE [LUT_SoilMapUnits]
 (
	[ID]			Long Integer NOT NULL,
	[musym]			Text (255),
	[muname]			Text (255)
);

CREATE TABLE [LUT_SPZone]
 (
	[ID]			Long Integer NOT NULL,
	[Zone]			Text (20)
);

CREATE TABLE [LUT_Station_ReadType]
 (
	[ID]			Long Integer NOT NULL,
	[LoggerType]			Text (255),
	[ReadType]			Text (10),
	[IDRT]			Long Integer,
	[LoggerTypeShort]			Text (255),
	[IsWQ]			Boolean NOT NULL,
	[SortOrder]			Long Integer
);

CREATE TABLE [LUT_Station_ReqMeas]
 (
	[TypeID]			Long Integer,
	[ReadTypeID]			Long Integer,
	[InstTypeID]			Long Integer,
	[ReqMPE]			Boolean NOT NULL,
	[ReqMPO]			Boolean NOT NULL,
	[ReqSO]			Boolean NOT NULL,
	[ReqLSE]			Boolean NOT NULL,
	[ViewOrder]			Long Integer,
	CONSTRAINT fk_reqmeas_type FOREIGN KEY ([TypeID]) REFERENCES [LUT_Station_Type] ([ID]),
	CONSTRAINT fk_reqmeas_readtype FOREIGN KEY ([ReadTypeID]) REFERENCES [LUT_Station_ReadType] ([ID]),
	CONSTRAINT fk_reqmeas_insttype FOREIGN KEY ([InstTypeID]) REFERENCES [LUT_Station_InstType] ([ID])
);

CREATE TABLE [LUT_Station_Type]
 (
	[ID]			Long Integer NOT NULL,
	[Type]			Text (255),
	[ShortType]			Text (10)
);

CREATE TABLE [LUT_Station_Units]
 (
	[ID]			Long Integer NOT NULL,
	[Units_Reading]			Text (255) NOT NULL,
	[Conv_Factor]			Single
);

CREATE TABLE [LUT_Status]
 (
	[ID]			Long Integer,
	[Status]			Text (255)
);

CREATE TABLE [LUT_SurficialUnits]
 (
	[ID]			Long Integer NOT NULL,
	[Code]			Text (255),
	[Description]			Text (255)
);

CREATE TABLE [LUTC_Initials]
 (
	[Initials]			Text (3),
	[FirstName]			Text (25),
	[LastName]			Text (25)
);

CREATE TABLE [MPO_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_mporead_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_mporead_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_mporead_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [PPT_Logger]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[ImportID]			Long Integer NOT NULL,
	[Level]			Double,
	[xRec]			Boolean NOT NULL,
	[xWL]			Boolean NOT NULL,
	[rInitials]			Text (3),
	[rDate]			DateTime,
	CONSTRAINT fk_pptlogger_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_pptlogger_import FOREIGN KEY ([ImportID]) REFERENCES [Imports] ([ID]),
	CONSTRAINT fk_pptlogger_rinitials FOREIGN KEY ([rInitials]) REFERENCES [LUTC_Initials] ([Initials])
);

CREATE TABLE [Projects]
 (
	[ID]			Long Integer,
	[ISGS_Num]			Text (10),
	[IDOT_Name]			Text (255),
	[SeqCode]			Text (255),
	[ISGS_Name]			Text (255),
	[BeginDT]			DateTime,
	[EndDT]			DateTime,
	[FA_num]			Text (255),
	[County]			Text (255),
	[TypeID]			Long Integer,
	CONSTRAINT fk_projects_type FOREIGN KEY ([TypeID]) REFERENCES [LUT_SiteType] ([ID])
);

CREATE TABLE [rcCalculation]
 (
	[Calculation]			Text (255),
	[ValidSW]			Boolean NOT NULL,
	[ValidGW]			Boolean NOT NULL,
	[SortOrder]			Long Integer
);

CREATE TABLE [ST_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_stread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_stread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_stread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [Stations]
 (
	[ID]			Long Integer,
	[ProjectID]			Long Integer NOT NULL,
	[TypeID]			Long Integer NOT NULL,
	[Code]			Text (255),
	[BeginDT]			DateTime,
	[EndDT]			DateTime,
	[StaName]			Text (255) NOT NULL,
	[LabelAlt]			Text (255),
	[Longitude]			Double,
	[Latitude]			Double,
	[LocationTypeID]			Long Integer,
	[Initials]			Text (3) NOT NULL,
	[InstTypeID]			Long Integer,
	[InstUnitsID]			Long Integer,
	[StationTypeID]			Long Integer,
	[ISGS_ID]			Text (12),
	[BorDT]			DateTime,
	[BorMethodID]			Long Integer,
	[Comment]			Text (255),
	CONSTRAINT fk_stations_project FOREIGN KEY ([ProjectID]) REFERENCES [Projects] ([ID]),
	CONSTRAINT fk_stations_type FOREIGN KEY ([TypeID]) REFERENCES [LUT_Station_Type] ([ID]),
	CONSTRAINT fk_stations_locationtype FOREIGN KEY ([LocationTypeID]) REFERENCES [LUT_LocationType] ([ID]),
	CONSTRAINT fk_stations_initials FOREIGN KEY ([Initials]) REFERENCES [LUTC_Initials] ([Initials]),
	CONSTRAINT fk_stations_insttype FOREIGN KEY ([InstTypeID]) REFERENCES [LUT_Station_InstType] ([ID]),
	CONSTRAINT fk_stations_instunits FOREIGN KEY ([InstUnitsID]) REFERENCES [LUT_Station_Units] ([ID]),
	CONSTRAINT fk_stations_stationtype FOREIGN KEY ([StationTypeID]) REFERENCES [LUT_Station_ReadType] ([ID]),
	CONSTRAINT fk_stations_bormethod FOREIGN KEY ([BorMethodID]) REFERENCES [LUT_BoringMethod] ([ID])
);

CREATE TABLE [STM_Logger]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[ImportID]			Long Integer NOT NULL,
	[Level1]			Double,
	[Level2]			Double,
	[Level3]			Double,
	[Level4]			Double,
	[Level5]			Double,
	[Temperature]			Double,
	[StatusID]			Long Integer,
	[AscNo]			Long Integer,
	[xWL]			Boolean NOT NULL,
	[xRec]			Boolean NOT NULL,
	[rInitials]			Text (3),
	[rDate]			DateTime,
	CONSTRAINT fk_stmlogger_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_stmlogger_import FOREIGN KEY ([ImportID]) REFERENCES [Imports] ([ID]),
	CONSTRAINT fk_stmlogger_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID]),
	CONSTRAINT fk_stmlogger_rinitials FOREIGN KEY ([rInitials]) REFERENCES [LUTC_Initials] ([Initials])
);

CREATE TABLE [SW_Logger]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[ImportID]			Long Integer NOT NULL,
	[Level]			Double,
	[Temperature]			Double,
	[xWL]			Boolean NOT NULL,
	[xT]			Boolean NOT NULL,
	[rInitials]			Text (3),
	[rDate]			DateTime,
	CONSTRAINT fk_swlogger_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_swlogger_import FOREIGN KEY ([ImportID]) REFERENCES [Imports] ([ID]),
	CONSTRAINT fk_swlogger_rinitials FOREIGN KEY ([rInitials]) REFERENCES [LUTC_Initials] ([Initials])
);

CREATE TABLE [SW_LoggerAux]
 (
	[DateTime]			DateTime NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[DO]			Double,
	[DOS]			Double,
	[ORP]			Long Integer,
	[PH]			Double,
	[SPC]			Double,
	[TRB]			Double,
	[TDS]			Double,
	[xDO]			Boolean NOT NULL,
	[xDOS]			Boolean NOT NULL,
	[xORP]			Boolean NOT NULL,
	[xPH]			Boolean NOT NULL,
	[xSPC]			Boolean NOT NULL,
	[xTRB]			Boolean NOT NULL,
	[xTDS]			Boolean NOT NULL,
	CONSTRAINT fk_swloggeraux_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_swloggeraux_record FOREIGN KEY ([DateTime], [StationID]) REFERENCES [SW_Logger] ([DateTime], [StationID])
);

CREATE TABLE [temp_REV_Flg_Summary]
 (
	[ID]			Long Integer,
	[TypeID]			Long Integer,
	[Code]			Text (255),
	[PhyWL]			Long Integer,
	[PhyTemp]			Long Integer,
	[OLTemp]			Long Integer,
	[DriftWLE]			Long Integer,
	[DriftChk]			Long Integer,
	[Total]			Long Integer,
	CONSTRAINT fk_temprev_type FOREIGN KEY ([TypeID]) REFERENCES [LUT_Station_Type] ([ID])
);

CREATE TABLE [Visits]
 (
	[ID]			Long Integer,
	[ProjectID]			Long Integer NOT NULL,
	[DT]			DateTime NOT NULL,
	[By]			Text (10) NOT NULL,
	[Note]			Text (255),
	[ReviewedBy]			Text (3),
	[ReviewedDate]			Text (255),
	CONSTRAINT fk_visits_project FOREIGN KEY ([ProjectID]) REFERENCES [Projects] ([ID]),
	CONSTRAINT fk_visits_reviewedby FOREIGN KEY ([ReviewedBy]) REFERENCES [LUTC_Initials] ([Initials])
);

CREATE TABLE [WellConstruction]
 (
	[ID]			Long Integer,
	[StationID]			Long Integer NOT NULL,
	[WellName]			Text (255),
	[DT]			DateTime,
	[Diameter]			Double,
	[HoleDepth]			Double,
	[WellTotalLength]			Double,
	[StickupInit]			Double,
	[StickupCutoff]			Double,
	[ScreenBot]			Double,
	[ScreenTop]			Double,
	[SandpackTopDepth]			Double,
	[BentoniteTopDepth]			Double,
	[SoilpackTopDepth]			Double,
	[GeologicMaterial]			Text (255),
	[GeologicMapUnitID]			Long Integer,
	[SoilMapUnitID]			Long Integer,
	[Comment]			Text (255),
	CONSTRAINT fk_wellconstruction_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_wellconstruction_geologic FOREIGN KEY ([GeologicMapUnitID]) REFERENCES [LUT_SurficialUnits] ([ID]),
	CONSTRAINT fk_wellconstruction_soil FOREIGN KEY ([SoilMapUnitID]) REFERENCES [LUT_SoilMapUnits] ([ID])
);

CREATE TABLE [WL_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[Level]			Double,
	[TimeOfWL]			DateTime,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_wlread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_wlread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_wlread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [XLFormInfo]
 (
	[ID]			Long Integer,
	[TableName]			Text (255),
	[DataName]			Text (255)
);

CREATE TABLE [GeologicDescriptions]
 (
	[ID]			Long Integer,
	[StationID]			Long Integer NOT NULL,
	[WHO]			Text (255),
	[INTERP_DATE]			DateTime,
	[TOP]			Double,
	[BOTTOM]			Double,
	[DRILL_COMMENTS]			Text (255),
	[MATERIAL]			Text (255),
	[MATERIALS_COMMENT]			Text (255),
	[STRATIFICATION]			Text (255),
	[STRAT_COMMENTS]			Text (255),
	[TEXTURE]			Text (255),
	[TEXTURE_COMMENTS]			Text (255),
	[COLOR_MUNSELL]			Text (255),
	[COLOR_DESCRIPTIVE]			Text (255),
	[COLOR_COMMENT]			Text (255),
	[REACTIVITY]			Text (255),
	[REACT_COMMENTS]			Text (255),
	[REDOX_FEATURES]			Text (255),
	[REDOX_COMMENTS]			Text (255),
	[SORTING]			Text (255),
	[MOISTURE]			Text (255),
	[CONSISTENCY]			Text (255),
	[PLASTICITY]			Text (255),
	[ORGANICS]			Text (255),
	[BURROWS]			Text (255),
	[COATINGS]			Text (255),
	[PEDOLOGICAL_UNIT]			Text (255),
	[FRACTURES]			Text (255),
	[STRUCTURE]			Text (255),
	[STRUCT_COMMENTS]			Text (255),
	[CLAST_QUANTITY]			Text (255),
	[CLAST_ROUNDING]			Text (255),
	[CLAST_SIZE]			Text (255),
	[CLAST_DESCRIPTION]			Text (255),
	[CLAST_COMMENTS]			Text (255),
	[CONTACT]			Text (255),
	[CONTACT_COMMENTS]			Text (255),
	[DEPOSITIONAL_ENVIRONS]			Text (255),
	[LITHOLOGY]			Text (255),
	[FORMATION]			Text (255),
	[MAP_UNIT]			Text (255),
	CONSTRAINT fk_geologicdesc_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID])
);

CREATE TABLE [LSE_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_lseread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_lseread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_lseread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [LUT_Datum]
 (
	[ID]			Long Integer NOT NULL,
	[Datum]			Text (10)
);

CREATE TABLE [LUT_Station_InstType]
 (
	[ID]			Long Integer,
	[InstType]			Text (255),
	[WLE_Equation]			Text (1)
);

CREATE TABLE [MPE_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_mperead_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_mperead_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_mperead_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [SO_Read]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_soread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_soread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_soread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);

CREATE TABLE [SW_Read (placeholder)]
 (
	[VisitID]			Long Integer NOT NULL,
	[StationID]			Long Integer NOT NULL,
	[Level]			Double,
	[StatusID]			Long Integer,
	[Note]			Text (255),
	CONSTRAINT fk_swread_visit FOREIGN KEY ([VisitID]) REFERENCES [Visits] ([ID]),
	CONSTRAINT fk_swread_station FOREIGN KEY ([StationID]) REFERENCES [Stations] ([ID]),
	CONSTRAINT fk_swread_status FOREIGN KEY ([StatusID]) REFERENCES [LUT_Status] ([ID])
);
