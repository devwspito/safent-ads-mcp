import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient, ApiRequestError } from "@/api/client";
import {
  assetKindSchema,
  brandDraftSchema,
  brandKitSchema,
  confirmBrandDraftInputSchema,
  updateBrandClaimsInputSchema,
  uploadedAssetCandidateSchema,
  type AssetKind,
  type BrandDraft,
  type BrandKit,
  type ConfirmBrandDraftInput,
  type UpdateBrandClaimsInput,
} from "@/api/schemas/brand";

/** `GET /brand`/`GET /brand/draft` responden 404 mientras no hay kit/borrador (rest-api.md §Marca):
 * un estado esperado del flujo, no un fallo de red — se resuelve a `null` en vez de burbujear como error. */
async function nullOn404<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) return null;
    throw error;
  }
}

function invalidateBrand(queryClient: ReturnType<typeof useQueryClient>, businessId: string) {
  void queryClient.invalidateQueries({ queryKey: ["brand-kit", businessId] });
  void queryClient.invalidateQueries({ queryKey: ["brand-draft", businessId] });
}

export function useBrandKit(businessId: string) {
  return useQuery<BrandKit | null>({
    queryKey: ["brand-kit", businessId],
    queryFn: () => nullOn404(() => apiClient.get("/brand", brandKitSchema, { business_id: businessId })),
    enabled: Boolean(businessId),
  });
}

export function useBrandDraft(businessId: string) {
  return useQuery<BrandDraft | null>({
    queryKey: ["brand-draft", businessId],
    queryFn: () => nullOn404(() => apiClient.get("/brand/draft", brandDraftSchema, { business_id: businessId })),
    enabled: Boolean(businessId),
  });
}

export function useDiscoverBrand(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => apiClient.post("/brand/discover", brandDraftSchema, { url }, { business_id: businessId }),
    onSuccess: (draft) => queryClient.setQueryData(["brand-draft", businessId], draft),
  });
}

export function useUploadBrandAsset(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { kind: AssetKind; file: File }) => {
      const formData = new FormData();
      formData.append("file", input.file);
      return apiClient.postForm("/brand/assets", uploadedAssetCandidateSchema, formData, {
        business_id: businessId,
        kind: assetKindSchema.parse(input.kind),
      });
    },
    onSuccess: () => invalidateBrand(queryClient, businessId),
  });
}

export function useConfirmBrandDraft(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ConfirmBrandDraftInput) =>
      apiClient.post("/brand/confirm", brandKitSchema, confirmBrandDraftInputSchema.parse(input), { business_id: businessId }),
    onSuccess: (kit) => {
      queryClient.setQueryData(["brand-kit", businessId], kit);
      invalidateBrand(queryClient, businessId);
    },
  });
}

export function useUpdateBrandClaims(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: UpdateBrandClaimsInput) =>
      apiClient.put("/brand/claims", brandKitSchema, updateBrandClaimsInputSchema.parse(input), { business_id: businessId }),
    onSuccess: (kit) => queryClient.setQueryData(["brand-kit", businessId], kit),
  });
}
