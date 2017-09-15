////////////////////////////////////////////////////////////////////////////////////////////////////
//
//  Project:  Embedded Learning Library (ELL)
//  File:     IRMapCompiler.cpp (model)
//  Authors:  Umesh Madan, Chuck Jacobs
//
////////////////////////////////////////////////////////////////////////////////////////////////////

#include "IRMapCompiler.h"
#include "CompilableNode.h"
#include "CompilableNodeUtilities.h"
#include "IRModelProfiler.h"
#include "OutputNode.h"

// emitters
#include "EmitterException.h"
#include "Variable.h"

namespace ell
{
namespace model
{
    IRMapCompiler::IRMapCompiler()
        : IRMapCompiler(MapCompilerParameters{})
    {
    }

    IRMapCompiler::IRMapCompiler(const MapCompilerParameters& settings)
        : MapCompiler(settings), _moduleEmitter(settings.moduleName), _profiler()
    {
        _moduleEmitter.SetCompilerParameters(settings.compilerSettings);
        _nodeRegions.emplace_back();
    }

    void IRMapCompiler::EnsureValidMap(DynamicMap& map)
    {
        if (map.NumInputPorts() != 1)
        {
            throw utilities::InputException(utilities::InputExceptionErrors::invalidArgument, "Compiled maps must have a single input");
        }

        if (map.NumOutputPorts() != 1)
        {
            throw utilities::InputException(utilities::InputExceptionErrors::invalidArgument, "Compiled maps must have a single output");
        }

        // if output isn't a simple port, add an output node to model
        auto out = map.GetOutput(0);
        ell::math::TensorShape shape{ out.Size(),1,1 }; // default shape from PortElementsBase::Size()
        auto outNodes = map.GetOutputNodes();
        if (outNodes.size() > 0) {
            shape = outNodes[0]->GetShape();
        }
        if (!out.IsFullPortOutput())
        {
            model::OutputNodeBase* outputNode = nullptr;
            switch (out.GetPortType())
            {
                case model::Port::PortType::boolean:
                    outputNode = map.GetModel().AddNode<model::OutputNode<bool>>(model::PortElements<bool>(out), shape);
                    break;
                case model::Port::PortType::integer:
                    outputNode = map.GetModel().AddNode<model::OutputNode<int>>(model::PortElements<int>(out), shape);
                    break;
                case model::Port::PortType::bigInt:
                    outputNode = map.GetModel().AddNode<model::OutputNode<int64_t>>(model::PortElements<int64_t>(out), shape);
                    break;
                case model::Port::PortType::smallReal:
                    outputNode = map.GetModel().AddNode<model::OutputNode<float>>(model::PortElements<float>(out), shape);
                    break;
                case model::Port::PortType::real:
                    outputNode = map.GetModel().AddNode<model::OutputNode<double>>(model::PortElements<double>(out), shape);
                    break;
                default:
                    throw utilities::InputException(utilities::InputExceptionErrors::typeMismatch);
            }

            map.ResetOutput(0, outputNode->GetOutputPort());
        }
    }

    std::string IRMapCompiler::GetNamespacePrefix() const
    {
        return GetModule().GetModuleName();
    }

    std::string IRMapCompiler::GetPredictFunctionName() const
    {
        return GetMapCompilerParameters().mapFunctionName;
    }

    IRCompiledMap IRMapCompiler::Compile(DynamicMap map)
    {
        EnsureValidMap(map);
        model::TransformContext context{ this, [this](const model::Node& node) { return node.IsCompilable(this) ? model::NodeAction::compile : model::NodeAction::refine; } };
        map.Refine(context);

        // Now the model ready for compiling
        if (GetMapCompilerParameters().profile)
        {
            GetModule().AddPreprocessorDefinition(GetNamespacePrefix() + "_PROFILING", "1");
        }
        _profiler = { GetModule(), map.GetModel(), GetMapCompilerParameters().profile };
        _profiler.EmitInitialization();

        // Now we have the refined map, compile it
        CompileMap(map, GetPredictFunctionName());

        // Emit runtime model APIs
        EmitModelAPIFunctions(map);

        // Finish any profiling stuff we need to do and emit functions
        _profiler.EmitModelProfilerFunctions();

        auto module = std::make_unique<emitters::IRModuleEmitter>(std::move(_moduleEmitter));
        module->SetTargetTriple(GetCompilerParameters().targetDevice.triple);
        module->SetTargetDataLayout(GetCompilerParameters().targetDevice.dataLayout);
        return IRCompiledMap(std::move(map), GetMapCompilerParameters().mapFunctionName, std::move(module));
    }

    void IRMapCompiler::EmitModelAPIFunctions(const DynamicMap& map)
    {
        EmitGetInputSizeFunction(map);
        EmitGetOutputSizeFunction(map);
        EmitGetNumNodesFunction(map);
        EmitShapeEnum();
        EmitGetInputShapeFunction(map);
        EmitGetOutputShapeFunction(map);
    }

    void IRMapCompiler::EmitGetInputSizeFunction(const DynamicMap& map)
    {
        auto& context = _moduleEmitter.GetLLVMContext();
        auto int32Type = llvm::Type::getInt32Ty(context);

        auto function = _moduleEmitter.BeginFunction(GetNamespacePrefix() + "_GetInputSize", int32Type);
        function.IncludeInHeader();
        function.Return(function.Literal(static_cast<int>(map.GetInputSize())));
        _moduleEmitter.EndFunction();
    }


    void IRMapCompiler::EmitGetOutputSizeFunction(const DynamicMap& map)
    {
        auto& context = _moduleEmitter.GetLLVMContext();
        auto int32Type = llvm::Type::getInt32Ty(context);

        auto function = _moduleEmitter.BeginFunction(GetNamespacePrefix() + "_GetOutputSize", int32Type);
        function.IncludeInHeader();
        function.Return(function.Literal(static_cast<int>(map.GetOutputSize())));
        _moduleEmitter.EndFunction();
    }

    const char* TensorShapeName = "TensorShape";

    void IRMapCompiler::EmitShapeEnum()
    {
        auto shapeType = _moduleEmitter.GetStruct(TensorShapeName);
        if (shapeType == nullptr) {
            auto int32Type = ell::emitters::VariableType::Int32;
            emitters::NamedVariableTypeList namedFields = { { "rows", int32Type },{ "columns", int32Type },{ "channels" , int32Type } };
            auto shapeType = _moduleEmitter.DeclareStruct(TensorShapeName, namedFields);
            _moduleEmitter.IncludeTypeInHeader(shapeType->getName());
        }
    }

    void IRMapCompiler::EmitShapeConditionals(emitters::IRFunctionEmitter& fn, std::vector<ell::math::TensorShape> shapes)
    {
        auto shapeType = _moduleEmitter.GetStruct(TensorShapeName);
        auto arguments = fn.Arguments().begin();
        auto indexArgument = &(*arguments++);
        indexArgument->setName("index");
        auto shapeArgument = &(*arguments++);
        shapeArgument->setName("shape");
        auto& emitter = _moduleEmitter.GetIREmitter();
        auto& irBuilder = emitter.GetIRBuilder();
        auto rowsPtr = irBuilder.CreateInBoundsGEP(shapeType, shapeArgument, { fn.Literal(0), fn.Literal(0) });
        rowsPtr->setName("rows");
        auto columnsPtr = irBuilder.CreateInBoundsGEP(shapeType, shapeArgument, { fn.Literal(0), fn.Literal(1) });
        columnsPtr->setName("columns");
        auto channelsPtr = irBuilder.CreateInBoundsGEP(shapeType, shapeArgument, { fn.Literal(0), fn.Literal(2) });
        channelsPtr->setName("channels");
        auto pMainBlock = fn.GetCurrentBlock();

        std::vector<llvm::BasicBlock*> blocks;
        blocks.push_back(pMainBlock);
        // create the final block in the case the index is out of range or there are no shapes and reutrn empty TensorShape.
        auto pDoneBlock = fn.BeginBlock("NoMatchBlock");
        {
            fn.Store(rowsPtr, fn.Literal(0));
            fn.Store(columnsPtr, fn.Literal(0));
            fn.Store(channelsPtr, fn.Literal(0));
            fn.Return();
        }

        if (shapes.size() > 0)
        {
            int index = 0;
            for (auto ptr = shapes.begin(), end = shapes.end(); ptr != end; ptr++, index++) {
                std::string labelSuffix = std::to_string(index);

                auto followBlock = fn.BeginBlock("FollowBlock" + labelSuffix);
                auto thenBlock = fn.BeginBlock("ThenBlock" + labelSuffix);
                {
                    math::TensorShape shape = *ptr;
                    fn.Store(rowsPtr, fn.Literal((int)shape.rows));
                    fn.Store(columnsPtr, fn.Literal((int)shape.columns));
                    fn.Store(channelsPtr, fn.Literal((int)shape.channels));
                    fn.Return();
                }
                auto elseBlock = fn.BeginBlock("ElseBlock" + labelSuffix);
                {
                    fn.Branch(followBlock);
                }
                auto conditionBlock = fn.BeginBlock("IfBlock" + labelSuffix);
                {
                    auto compare = new llvm::ICmpInst(*conditionBlock, llvm::ICmpInst::ICMP_EQ, indexArgument, fn.Literal(index));
                    llvm::BranchInst::Create(thenBlock, elseBlock, compare, conditionBlock);
                }
                blocks.push_back(conditionBlock);
                blocks.push_back(followBlock);
            }
            fn.SetCurrentBlock(blocks[blocks.size() - 1]);
            fn.Branch(pDoneBlock);
        }

        fn.SetCurrentBlock(pMainBlock);
        if (blocks.size() > 1)
        {
            fn.Branch(blocks[1]); // jump to first statement.
        }

        blocks.push_back(pDoneBlock);
        // ensure all blocks are properly chained with branch instructions and inserted into the function BasicBlockList.
        fn.ConcatenateBlocks(blocks); 
    }


    // This is the type of code we are trying to generate for the GetInputShape and GetOutputShape functions:
    // 
    // void foo_GetInputShape(int index, struct TensorShape* s)
    // {
    //     if (index == 0) {
    //         s->rows = 224;
    //         s->columns = 224;
    //         s->channels = 3;
    //         return;
    //     }
    //     if (index == 1) {
    //         s->rows = 224;
    //         s->columns = 224;
    //         s->channels = 3;
    //         return;
    //     }
    //     s->rows = 0;
    //     s->columns = 0;
    //     s->channels = 0;
    // }
    // 

    void IRMapCompiler::EmitGetInputShapeFunction(const DynamicMap& map)
    {
        // We have to create this interface because LLVM cannot reliably return structures on the stack.
        // void darknet_GetInputShape(struct TensorShape* shape, int index)
        auto shapeType = _moduleEmitter.GetStruct(TensorShapeName);
        auto& context = _moduleEmitter.GetLLVMContext();
        auto voidType = llvm::Type::getVoidTy(context);
        auto int32Type = llvm::Type::getInt32Ty(context);
        const std::vector<llvm::Type*> parameters = { { int32Type }, { shapeType->getPointerTo() } };
        auto fn = _moduleEmitter.BeginFunction(GetNamespacePrefix() + "_GetInputShape", voidType, parameters);
        fn.IncludeInHeader();
        auto nodes = map.GetInputNodes();
        std::vector<math::TensorShape> shapes;
        for (auto ptr = nodes.begin(), end = nodes.end(); ptr != end; ptr++) {
            const ell::model::InputNodeBase* node = *ptr;
            shapes.push_back(node->GetShape());
        }
        EmitShapeConditionals(fn, shapes);
        _moduleEmitter.EndFunction();
    }

    void IRMapCompiler::EmitGetOutputShapeFunction(const DynamicMap& map)
    {
        // We have to create this interface because LLVM cannot reliably return structures on the stack.
        // void darknet_GetInputShape(struct TensorShape* shape, int index)
        auto shapeType = _moduleEmitter.GetStruct(TensorShapeName);
        auto& context = _moduleEmitter.GetLLVMContext();
        auto voidType = llvm::Type::getVoidTy(context);
        auto int32Type = llvm::Type::getInt32Ty(context);
        const std::vector<llvm::Type*> parameters = { { int32Type },{ shapeType->getPointerTo() } };
        auto fn = _moduleEmitter.BeginFunction(GetNamespacePrefix() + "_GetOutputShape", voidType, parameters);
        fn.IncludeInHeader();
        auto nodes = map.GetOutputNodes();
        std::vector<math::TensorShape> shapes;
        for (auto ptr = nodes.begin(), end = nodes.end(); ptr != end; ptr++) {
            const ell::model::OutputNodeBase* node = *ptr;
            shapes.push_back(node->GetShape());
        }
        EmitShapeConditionals(fn, shapes);
        _moduleEmitter.EndFunction();
    }

    void IRMapCompiler::EmitGetNumNodesFunction(const DynamicMap& map)
    {
        auto& context = _moduleEmitter.GetLLVMContext();
        auto int32Type = llvm::Type::getInt32Ty(context);
        int numNodes = map.GetModel().Size();

        auto function = _moduleEmitter.BeginFunction(GetNamespacePrefix() + "_GetNumNodes", int32Type);
        function.IncludeInHeader();
        function.Return(function.Literal(numNodes));
        _moduleEmitter.EndFunction();
    }

    //
    // Node implementor methods:
    //

    llvm::Value* IRMapCompiler::EnsurePortEmitted(const InputPortBase& port)
    {
        auto portElement = port.GetInputElement(0);
        return EnsurePortElementEmitted(portElement);
    }

    llvm::Value* IRMapCompiler::EnsurePortEmitted(const OutputPortBase& port)
    {
        auto pVar = GetOrAllocatePortVariable(port);
        return GetModule().EnsureEmitted(*pVar);
    }

    llvm::Value* IRMapCompiler::EnsurePortElementEmitted(const PortElementBase& element)
    {
        auto pVar = GetVariableForElement(element);
        if (pVar == nullptr)
        {
            throw emitters::EmitterException(emitters::EmitterError::notSupported, "Variable for output port not found");
        }
        return GetModule().EnsureEmitted(*pVar);
    }

    void IRMapCompiler::OnBeginCompileModel(const Model& model)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();
        if (currentFunction.GetCurrentRegion() == nullptr) // TODO: put this check in GetCurrentFunction()
        {
            currentFunction.AddRegion(currentFunction.GetCurrentBlock());
        }

        // Tag the model function for declaration in the generated headers
        currentFunction.IncludeInHeader();
        currentFunction.IncludeInPredictInterface();

        _profiler.StartModel(currentFunction);
    }

    void IRMapCompiler::OnEndCompileModel(const Model& model)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();
        _profiler.EndModel(currentFunction);
    }

    void IRMapCompiler::OnBeginCompileNode(const Node& node)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();
        if (currentFunction.GetCurrentRegion() == nullptr)
        {
            currentFunction.AddRegion(currentFunction.GetCurrentBlock());
        }

        _profiler.InitNode(currentFunction, node);
        _profiler.StartNode(currentFunction, node);
    }

    void IRMapCompiler::OnEndCompileNode(const Node& node)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();
        assert(currentFunction.GetCurrentRegion() != nullptr);

        _profiler.EndNode(currentFunction, node);

        auto pCurBlock = currentFunction.GetCurrentBlock();
        if (pCurBlock != currentFunction.GetCurrentRegion()->End())
        {
            currentFunction.GetCurrentRegion()->SetEnd(pCurBlock);
        }
    }

    void IRMapCompiler::PushScope()
    {
        MapCompiler::PushScope();
        _nodeRegions.emplace_back();
    }

    void IRMapCompiler::PopScope()
    {
        MapCompiler::PopScope();
        assert(_nodeRegions.size() > 0);
        _nodeRegions.pop_back();
    }

    NodeMap<emitters::IRBlockRegion*>& IRMapCompiler::GetCurrentNodeBlocks()
    {
        assert(_nodeRegions.size() > 0);
        return _nodeRegions.back();
    }

    const Node* IRMapCompiler::GetUniqueParent(const Node& node)
    {
        auto inputs = node.GetInputPorts();
        const Node* pParentNode = nullptr;
        emitters::IRBlockRegion* pParentRegion = nullptr;
        for (auto input : inputs)
        {
            for (auto parentNode : input->GetParentNodes())
            {
                if (!HasSingleDescendant(*parentNode))
                {
                    return nullptr;
                }
                emitters::IRBlockRegion* pNodeRegion = GetCurrentNodeBlocks().Get(*parentNode);
                if (pNodeRegion != nullptr)
                {
                    if (pParentRegion != nullptr && pNodeRegion != pParentRegion)
                    {
                        return nullptr;
                    }
                    pParentRegion = pNodeRegion;
                    pParentNode = parentNode;
                }
            }
        }
        return pParentNode;
    }

    void IRMapCompiler::NewNodeRegion(const Node& node)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();
        auto pBlock = currentFunction.Block(IdString(node));
        assert(pBlock != nullptr && "Got null new block");
        currentFunction.SetCurrentBlock(pBlock);
        auto currentRegion = currentFunction.AddRegion(pBlock);
        GetCurrentNodeBlocks().Set(node, currentRegion);

        if (GetMapCompilerParameters().compilerSettings.includeDiagnosticInfo)
        {
            currentFunction.Print(DiagnosticString(node) + '\n');
        }
    }

    bool IRMapCompiler::TryMergeNodeRegion(const Node& node)
    {
        auto pRegion = GetCurrentNodeBlocks().Get(node);
        if (pRegion == nullptr)
        {
            return false;
        }

        const Node* pParentNode = GetUniqueParent(node);
        if (pParentNode == nullptr)
        {
            return false;
        }

        return TryMergeNodeRegions(*pParentNode, node);
    }

    bool IRMapCompiler::TryMergeNodeRegions(const Node& dest, const Node& src)
    {
        emitters::IRBlockRegion* pDestRegion = GetCurrentNodeBlocks().Get(dest);
        if (pDestRegion == nullptr)
        {
            return false;
        }
        return TryMergeNodeIntoRegion(pDestRegion, src);
    }

    bool IRMapCompiler::TryMergeNodeIntoRegion(emitters::IRBlockRegion* pDestRegion, const Node& src)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();

        emitters::IRBlockRegion* pSrcRegion = GetCurrentNodeBlocks().Get(src);
        if (pSrcRegion == nullptr || pSrcRegion == pDestRegion)
        {
            return false;
        }

        GetModule().GetCurrentRegion()->SetEnd(currentFunction.GetCurrentBlock());
        currentFunction.ConcatRegions(pDestRegion, pSrcRegion);
        GetCurrentNodeBlocks().Set(src, pDestRegion);
        return true;
    }

    emitters::IRBlockRegion* IRMapCompiler::GetMergeableNodeRegion(const PortElementBase& element)
    {
        const Node* pNode = nullptr;
        if (HasSingleDescendant(element))
        {
            emitters::Variable* pVar = GetVariableForElement(element);
            if (pVar != nullptr && !pVar->IsLiteral())
            {
                pNode = element.ReferencedPort()->GetNode();
            }
        }

        return (pNode != nullptr) ? GetCurrentNodeBlocks().Get(*pNode) : nullptr;
    }

    llvm::LLVMContext& IRMapCompiler::GetLLVMContext()
    {
        return _moduleEmitter.GetLLVMContext();
    }

    //
    // Port variables
    //
    llvm::Value* IRMapCompiler::LoadPortVariable(const InputPortBase& port)
    {
        return LoadPortElementVariable(port.GetInputElement(0)); // Note: this fails on scalar input variables
    }

    llvm::Value* IRMapCompiler::LoadPortElementVariable(const PortElementBase& element)
    {
        auto& currentFunction = GetModule().GetCurrentFunction();

        // Error: if we pass in a single element from a range, we need to use startindex as part of the key for looking up the element. In fact, we should have a separate map for vector port and scalar element variables...
        emitters::Variable* pVar = GetVariableForElement(element);
        llvm::Value* pVal = GetModule().EnsureEmitted(*pVar);
        if (pVar->IsScalar())
        {
            if (pVar->IsLiteral())
            {
                return pVal;
            }
            else if (pVar->IsInputArgument())
            {
                return pVal;
            }
            else
            {
                return currentFunction.Load(pVal);
            }
        }

        // Else return an element from a vector (unless it was in fact passed in by value)
        auto valType = pVal->getType();
        bool needsDereference = valType->isPointerTy(); // TODO: Maybe this should be `isPtrOrPtrVectorTy()` or even `isPtrOrPtrVectorTy() || isArrayTy()`
        if (needsDereference)
        {
            return currentFunction.ValueAt(pVal, currentFunction.Literal((int)element.GetIndex()));
        }
        else
        {
            return pVal;
        }
    }

    emitters::Variable* IRMapCompiler::GetPortElementVariable(const PortElementBase& element)
    {
        emitters::Variable* pVar = GetVariableForElement(element);
        if (pVar == nullptr)
        {
            throw emitters::EmitterException(emitters::EmitterError::notSupported, "Variable for output port not found");
        }
        if (pVar->IsScalar() && element.GetIndex() > 0)
        {
            throw emitters::EmitterException(emitters::EmitterError::vectorVariableExpected);
        }
        else if (element.GetIndex() >= pVar->Dimension())
        {
            throw emitters::EmitterException(emitters::EmitterError::indexOutOfRange);
        }

        return pVar;
    }

    emitters::Variable* IRMapCompiler::GetPortVariable(const InputPortBase& port)
    {
        return GetPortElementVariable(port.GetInputElement(0)); // Note: Potential error: scalar vars passed by value won't work here
    }
}
}
