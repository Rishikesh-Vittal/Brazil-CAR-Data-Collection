from __future__ import annotations

from typing import Any


def _fix_text(value: str) -> str:
    if not value:
        return value
    for _ in range(2):
        if "Ã" not in value and "Â" not in value:
            break
        try:
            repaired = value.encode("latin-1").decode("utf-8")
        except UnicodeError:
            break
        if repaired == value:
            break
        value = repaired
    return value


def repair_mojibake(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: repair_mojibake(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, str):
        return _fix_text(value)
    return value


def build_normalized_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    payload = repair_mojibake(raw_payload)
    dados = payload.get("dados") or {}
    cabecalho = dados.get("cabecalho") or {}
    areas = dados.get("areas") or {}
    restricoes = dados.get("restricoes") or []
    return {
        "registration_status": cabecalho.get("statusImovel"),
        "registration_number": cabecalho.get("codigo"),
        "external_condition": cabecalho.get("condicaoAnalise"),
        "environmental_regularization_program_opt_in": cabecalho.get("aderiuPRA"),
        "property_area_ha": cabecalho.get("area"),
        "fiscal_modules": cabecalho.get("modulosFiscais"),
        "municipality": cabecalho.get("municipio"),
        "state": cabecalho.get("estado"),
        "centroid_longitude_decimal": cabecalho.get("centroideX"),
        "centroid_latitude_decimal": cabecalho.get("centroideY"),
        "centroid_longitude_dms": cabecalho.get("longitude"),
        "centroid_latitude_dms": cabecalho.get("latitude"),
        "registration_date": cabecalho.get("dataRegistro"),
        "analysis_date": cabecalho.get("dataAnalise"),
        "last_amendment_date": cabecalho.get("dataRetificacao"),
        "demonstrative_generated_at": cabecalho.get("dataDemonstrativo"),
        "had_amendment": cabecalho.get("houveRetificacao"),
        "car_message": payload.get("mensagem"),
        "car_status": payload.get("status"),
        "native_vegetation_area_ha": areas.get("areaRVN"),
        "consolidated_use_area_ha": areas.get("areaUsoConsolidado"),
        "administrative_servitude_area_ha": areas.get("areaServidaoAdministrativa"),
        "legal_reserve_status": areas.get("situacaoRL"),
        "legal_reserve_registered_area_ha": areas.get("areaRLA"),
        "legal_reserve_approved_not_registered_area_ha": areas.get("areaRLANA"),
        "legal_reserve_proposed_area_ha": areas.get("areaRLP"),
        "legal_reserve_recovery_area_ha": areas.get("areaRLRecompor"),
        "legal_reserve_declared_area_ha": areas.get("areaRLDeclarada"),
        "permanent_preservation_area_ha": areas.get("areaAPP"),
        "restricted_use_area_ha": areas.get("areaUsoRestrito"),
        "settlement_overlap_area_ha": areas.get("areaSobreposicaoAssentamento"),
        "other_property_overlap_area_ha": areas.get("areaSobreposicaoOutrosImoveis"),
        "indigenous_land_overlap_area_ha": areas.get("areaSobreposicaoTI"),
        "conservation_unit_overlap_area_ha": areas.get("areaSobreposicaoUC"),
        "restrictions_count": len(restricoes),
        "restrictions": restricoes,
    }
