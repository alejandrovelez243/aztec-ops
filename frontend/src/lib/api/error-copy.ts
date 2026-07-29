/**
 * Spanish copy for a failed API call — the one place a machine code becomes a
 * sentence a person reads.
 *
 * Every surface used to keep its own `Record<ApiErrorCode, string>`, which had
 * two costs: the same sentence was written five times, and adding a code to
 * the union broke five files at once instead of one. Worse, an exhaustive map
 * per page makes the *backend* growing a new error a frontend-wide edit, which
 * is the coupling the standards exist to prevent.
 *
 * `error.message` is never rendered: it is generated English prose meant for
 * logs (`docs/API.md` §4.2). Callers branch on `code`, and this module owns the
 * translation.
 */
import type { ApiError, ApiErrorCode } from "./errors";

/** What a surface shows when a read or a write fails. */
export interface ErrorCopy {
  /** One line naming what failed, in the operator's words. */
  title: string;
  /** What to do about it, or why it happened. */
  detail: string;
}

/**
 * Base copy per machine code, written for a *read* that failed.
 *
 * Exhaustive over `ApiErrorCode` on purpose: a code added to the union stops
 * the build here — one file — until somebody decides what an operator should
 * be told about it.
 */
const BY_CODE: Readonly<Record<ApiErrorCode, ErrorCopy>> = {
  network_error: {
    title: "No hay respuesta del servidor",
    detail: "Comprueba que el backend esté en marcha y vuelve a intentarlo.",
  },
  not_found: {
    title: "No encontramos esto",
    detail: "Pudo cambiar de código o haber sido archivado.",
  },
  validation_error: {
    title: "El servidor rechazó los datos",
    detail: "Revisa los campos marcados y vuelve a enviarlo.",
  },
  transition_not_allowed: {
    title: "Ese movimiento no es legal",
    detail: "El flujo de trabajo no permite ese cambio desde el estado actual.",
  },
  conflicting_state: {
    title: "Alguien más cambió esto",
    detail: "Vuelve a cargar para ver el estado actual.",
  },
  authentication_required: {
    title: "Tu sesión no está activa",
    detail: "Vuelve a iniciar sesión para continuar.",
  },
  invalid_token: {
    title: "Tu sesión expiró",
    detail: "Vuelve a iniciar sesión para continuar.",
  },
  invalid_credentials: {
    title: "Usuario o contraseña incorrectos",
    detail: "También puede ser que la cuenta esté desactivada.",
  },
  permission_denied: {
    title: "No tienes permiso para esta acción",
    detail: "Solo un responsable de operaciones puede hacerlo.",
  },
  domain_error: {
    title: "Una regla del negocio rechazó la acción",
    detail: "Revisa el estado del proyecto y vuelve a intentarlo.",
  },
  unknown_error: {
    title: "Algo falló inesperadamente",
    detail: "Vuelve a intentarlo; si sigue igual, revisa los registros.",
  },
};

/**
 * Copy for one failure, refined by what the error itself carries.
 *
 * The refinements are the reason this is a function and not just the map: a
 * rejected transition knows which moves *are* legal, a validation error knows
 * which field it refused, and a permission error knows which capability was
 * missing. Saying so turns a dead end into an instruction.
 */
export function errorCopy(error: ApiError): ErrorCopy {
  const base = BY_CODE[error.code];

  if (error.kind === "transition_not_allowed" && error.allowed.length > 0) {
    return {
      title: base.title,
      detail: `Desde el estado actual solo se puede pasar a: ${error.allowed.join(", ")}.`,
    };
  }

  if (error.kind === "validation") {
    const fields = Object.keys(error.fields);
    if (fields.length > 0) {
      return {
        title: base.title,
        detail: `Falta o es inválido: ${fields.join(", ")}.`,
      };
    }
  }

  if (error.kind === "permission_denied" && error.required === "ops_lead") {
    return {
      title: base.title,
      detail:
        "Ajustar la prioridad o recalcular el portafolio está reservado a un responsable de operaciones.",
    };
  }

  return base;
}

/** The `detail` alone, for surfaces that already render their own heading. */
export function errorDetail(error: ApiError): string {
  return errorCopy(error).detail;
}

/**
 * Copy from a bare code, for callers that kept only the discriminator.
 *
 * `ViewState`'s error arm carries `code` and `message` rather than the whole
 * `ApiError`, so a failed *read* reaches its renderer with the code alone —
 * which is enough, because the refinements above describe rejected *writes*
 * (which move is legal, which field was refused, which capability is missing)
 * and a read has none of those facts to offer. Write paths hold the real error
 * and call {@link errorCopy}.
 */
export function errorCopyByCode(code: ApiErrorCode): ErrorCopy {
  return BY_CODE[code];
}

/** The `detail` alone, from a bare code. */
export function errorDetailByCode(code: ApiErrorCode): string {
  return BY_CODE[code].detail;
}
