////////////////////////////////////////////////////////////////////////////////////////////////////
//
//  Project:  Embedded Learning Library (ELL)
//  File:     ValueScalarOperations.h (value)
//  Authors:  Kern Handa
//
////////////////////////////////////////////////////////////////////////////////////////////////////

#pragma once

// stl
#include <type_traits>

namespace ell
{
namespace value
{

    class Scalar;

    /// <summary> Arithmetic operators </summary>
    Scalar operator+(Scalar, Scalar);
    Scalar operator*(Scalar, Scalar);
    Scalar operator-(Scalar, Scalar);
    Scalar operator/(Scalar, Scalar);
    Scalar operator%(Scalar, Scalar);

    Scalar operator++(Scalar);
    Scalar operator++(Scalar, int);
    Scalar operator--(Scalar);
    Scalar operator--(Scalar, int);

    Scalar operator==(Scalar, Scalar);
    Scalar operator!=(Scalar, Scalar);
    Scalar operator<(Scalar, Scalar);
    Scalar operator<=(Scalar, Scalar);
    Scalar operator>(Scalar, Scalar);
    Scalar operator>=(Scalar, Scalar);

} // namespace value
} // namespace ell